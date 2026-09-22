"""
PDF TOC Splitter - Splits a PDF into sections based on its Table of Contents (bookmarks).

How sections are cut
  Each section starts on the page of its bookmark and ends on the page where the next
  bookmark starts. When the next topic begins partway down a page, the top of that page
  still belongs to the current topic, so that page is included in both sections.
  The page is left out only when the next topic starts at the very top of its page
  (detected from the bookmark's vertical position). See --boundary.

Performance
  - The source PDF is opened once per worker process, not once per section.
  - Each section is built by copying only its pages into a new PDF (fast, and it does
    not drag in unrelated pages through cross-reference links).
  - Sections are processed in parallel (--workers).
  - Ghostscript font subsetting is optional (--no-gs) and is the slowest step.

Resume
  Every file is written to "<name>.part" and renamed only when it is complete, so an
  interrupted run never leaves a broken PDF under a final name. Re-running the same
  command skips sections that already have a file. A small _split_info.json file in the
  output folder records the settings, so files from a different run or script version
  are never mistaken for valid output.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pikepdf
from pypdf import PdfReader

SCRIPT_VERSION = 2
INFO_FILE = "_split_info.json"

# Path to Ghostscript executable
GS_EXE = shutil.which("gswin64c") or shutil.which("gswin32c") or shutil.which("gs") or ""


# --------------------------------------------------------------------------------------
# TOC reading and section building
# --------------------------------------------------------------------------------------

def sanitize_filename(name: str, max_length: int = 80) -> str:
    """Convert a TOC title to a safe filename."""
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name)
    name = name[:max_length].strip(" .")
    return name or "untitled"


def _dest_top_fraction(reader: PdfReader, item, page_index: int):
    """
    Return how far down the page the bookmark points, as a fraction of page height
    (0.0 = top edge, 1.0 = bottom edge). Returns None when the bookmark has no position.
    """
    try:
        top = item.top
        if top is None:
            return None
        top = float(top)
        box = reader.pages[page_index].mediabox
        height = float(box.top) - float(box.bottom)
        if height <= 0:
            return None
        frac = (float(box.top) - top) / height
        return min(max(frac, 0.0), 1.0)
    except Exception:
        return None


def extract_toc(reader: PdfReader) -> list[dict]:
    """
    Extract TOC entries from PDF bookmarks via pypdf.
    Returns a flat list of {title, page, depth, top} in document order.
    'top' is the fraction down the page where the topic starts, or None if unknown.
    """
    entries = []

    def walk(outlines, depth=0):
        for item in outlines:
            if isinstance(item, list):
                walk(item, depth + 1)
            else:
                try:
                    page_index = reader.get_destination_page_number(item)
                    if page_index is not None:
                        entries.append({
                            "title": item.title,
                            "page": page_index,
                            "depth": depth,
                            "top": _dest_top_fraction(reader, item, page_index),
                        })
                except Exception:
                    pass  # Skip malformed/unresolvable entries

    if reader.outline:
        walk(reader.outline)

    # Stable sort: keeps outline order for topics that start on the same page
    entries.sort(key=lambda e: (e["page"], e["top"] if e["top"] is not None else 0.0))
    return entries


def build_sections(
    toc: list[dict],
    total_pages: int,
    top_level_only: bool,
    boundary: str = "auto",
    top_margin: float = 0.10,
) -> list[dict]:
    """
    Convert TOC entries into page ranges (0-based, inclusive).

    boundary:
      auto   - include the next topic's first page unless that topic starts within
               top_margin of the top of its page. Unknown position = include (no data loss).
      always - always include the next topic's first page (never loses content).
      never  - old behavior: stop on the page before the next topic (can lose content).
    """
    if top_level_only:
        toc = [e for e in toc if e["depth"] == 0]
    if not toc:
        return []

    sections = []
    for i, entry in enumerate(toc):
        start = entry["page"]
        if i + 1 < len(toc):
            nxt = toc[i + 1]
            if boundary == "never":
                end = nxt["page"] - 1
            elif boundary == "always":
                end = nxt["page"]
            else:
                starts_at_top = nxt["top"] is not None and nxt["top"] <= top_margin
                end = nxt["page"] - 1 if starts_at_top else nxt["page"]
        else:
            end = total_pages - 1
        end = max(min(end, total_pages - 1), start)
        sections.append({"title": entry["title"], "start": start, "end": end})

    return sections


def boundary_stats(toc: list[dict], top_level_only: bool, top_margin: float) -> tuple[int, int, int]:
    """Count topics that start at the top of a page, mid-page, or at an unknown position."""
    if top_level_only:
        toc = [e for e in toc if e["depth"] == 0]
    at_top = mid = unknown = 0
    for e in toc[1:]:
        if e["top"] is None:
            unknown += 1
        elif e["top"] <= top_margin:
            at_top += 1
        else:
            mid += 1
    return at_top, mid, unknown


def parse_selection(selection: str, total: int) -> list[int]:
    """
    Parse a selection string like "2,4" or "1-3,5" into a sorted list of 1-based indices.
    Raises ValueError for out-of-range or malformed input.
    """
    indices = set()
    for part in selection.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo, hi = int(lo.strip()), int(hi.strip())
            if lo < 1 or hi > total or lo > hi:
                raise ValueError(f"Range {lo}-{hi} is out of bounds (1-{total})")
            indices.update(range(lo, hi + 1))
        else:
            n = int(part)
            if n < 1 or n > total:
                raise ValueError(f"Index {n} is out of bounds (1-{total})")
            indices.add(n)
    return sorted(indices)


# --------------------------------------------------------------------------------------
# Writing (runs inside worker processes)
# --------------------------------------------------------------------------------------

_SRC = None     # pikepdf.Pdf opened once per worker
_USE_GS = False


def _init_worker(src_path: str, use_gs: bool):
    global _SRC, _USE_GS
    _SRC = pikepdf.open(src_path)
    _USE_GS = use_gs


def ghostscript_subset(in_path: Path, out_path: Path) -> bool:
    """Rewrite a PDF with subsetted fonts and optimized streams. Returns True on success."""
    if not GS_EXE:
        return False
    cmd = [
        GS_EXE,
        "-dBATCH", "-dNOPAUSE", "-dQUIET", "-dSAFER",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        "-dSubsetFonts=true",
        "-dEmbedAllFonts=true",
        "-dCompressFonts=true",
        "-dDetectDuplicateImages=true",
        f"-sOutputFile={out_path}",
        str(in_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def _fsync_file(path: Path):
    """Force file contents to disk so a power loss cannot leave a renamed but empty file."""
    try:
        with open(path, "rb+") as f:
            os.fsync(f.fileno())
    except OSError:
        pass


def _write_task(task: tuple) -> tuple:
    """
    Build one output file. task = (key, page_ranges, out_path_str).
    Returns (key, out_path_str, size_bytes, error_message_or_None, gs_failed).
    """
    key, page_ranges, out_path_str = task
    out_path = Path(out_path_str)
    stage_path = out_path.with_name(out_path.name + ".stage.part")
    part_path = out_path.with_name(out_path.name + ".part")
    gs_failed = False
    try:
        dst = pikepdf.new()
        for start, end in page_ranges:
            dst.pages.extend(_SRC.pages[start:end + 1])
        dst.save(
            stage_path,
            compress_streams=True,
            recompress_flate=False,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
        dst.close()

        if _USE_GS and ghostscript_subset(stage_path, part_path):
            stage_path.unlink(missing_ok=True)
        else:
            gs_failed = _USE_GS
            part_path.unlink(missing_ok=True)
            os.replace(stage_path, part_path)

        _fsync_file(part_path)
        os.replace(part_path, out_path)
        return key, out_path_str, out_path.stat().st_size, None, gs_failed
    except Exception as e:
        return key, out_path_str, 0, f"{type(e).__name__}: {e}", gs_failed
    finally:
        stage_path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# Resume support
# --------------------------------------------------------------------------------------

def is_complete(out_path: Path, expected_pages: int) -> bool:
    """True if out_path opens as a valid PDF with the expected page count."""
    try:
        if not out_path.exists() or out_path.stat().st_size == 0:
            return False
        with pikepdf.open(out_path) as pdf:
            return len(pdf.pages) == expected_pages
    except Exception:
        return False


def cleanup_partials(output_dir: Path) -> int:
    """Remove leftover .part files from an interrupted run."""
    removed = 0
    for p in output_dir.glob("*.part"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def check_run_info(output_dir: Path, info: dict, force: bool) -> bool:
    """
    Make sure existing files in output_dir were produced with the same settings.
    Returns True if it is safe to continue. Writes the info file for this run.
    """
    info_path = output_dir / INFO_FILE
    has_pdfs = any(p.suffix.lower() == ".pdf" for p in output_dir.iterdir()) if output_dir.exists() else False

    if has_pdfs and not force:
        old = None
        if info_path.exists():
            try:
                old = json.loads(info_path.read_text(encoding="utf-8"))
            except Exception:
                old = None
        if old != info:
            print(
                "Error: the output folder already has PDF files that were created by an older\n"
                "version of this script or with different settings. They may have missing pages.\n"
                f"  Folder: {output_dir}\n"
                "Use a new output folder with -o, or add --force to rebuild everything in place.",
                file=sys.stderr,
            )
            return False

    output_dir.mkdir(parents=True, exist_ok=True)
    info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return True


def plan_resume(
    filenames: list[str],
    page_counts: list[int],
    output_dir: Path,
    force: bool,
    verify_all: bool,
    verify_newest: int,
) -> set[int]:
    """
    Return the set of section indices (0-based) that can be skipped.
    One folder listing is used. Because files are only renamed into place once complete,
    an existing file is trusted. As a safety net, the most recently written files are
    opened and checked (they are the ones most likely affected by a crash).
    """
    if force or not output_dir.exists():
        return set()

    with os.scandir(output_dir) as it:
        existing = {e.name: e.stat().st_mtime for e in it if e.is_file() and e.name.lower().endswith(".pdf")}

    present = [i for i, fn in enumerate(filenames) if fn in existing]
    if not present:
        return set()

    if verify_all:
        print(f"Resume: verifying all {len(present)} existing file(s), this can take a while...")
        to_check = present
    else:
        to_check = sorted(present, key=lambda i: existing[filenames[i]], reverse=True)[:verify_newest]

    skip = set(present)
    bad = [i for i in to_check if not is_complete(output_dir / filenames[i], page_counts[i])]
    skip.difference_update(bad)

    print(f"Resume: {len(present)} of {len(filenames)} section file(s) already exist.")
    if bad:
        print(f"  {len(bad)} of the {len(to_check)} checked file(s) are incomplete and will be rebuilt.")
    print(f"  Skipping {len(skip)}, writing {len(filenames) - len(skip)}.")
    return skip


# --------------------------------------------------------------------------------------
# Main split logic
# --------------------------------------------------------------------------------------

def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def split_pdf(
    input_path: str,
    output_dir: str,
    top_level_only: bool = False,
    select: str = "",
    merge_selected: bool = False,
    prefix: str = "",
    dry_run: bool = False,
    force: bool = False,
    verify_all: bool = False,
    boundary: str = "auto",
    top_margin: float = 0.10,
    workers: int = 1,
    use_gs: bool = True,
) -> int:
    """
    Split the PDF at input_path by its TOC.
    Returns the number of files written plus skipped, or -1 on a fatal error.
    """
    input_path = Path(input_path).resolve()
    output_dir = Path(output_dir).resolve()

    if not input_path.exists():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return -1

    original_size = input_path.stat().st_size
    use_gs = use_gs and bool(GS_EXE)

    t0 = time.time()
    reader = PdfReader(str(input_path))
    total_pages = len(reader.pages)
    toc = extract_toc(reader)
    if not toc:
        print("No table of contents (bookmarks) found in this PDF. Cannot split.", file=sys.stderr)
        return -1

    print(f"Found {len(toc)} TOC entries across {total_pages} pages ({time.time() - t0:.1f}s).")
    at_top, mid, unknown = boundary_stats(toc, top_level_only, top_margin)
    print(
        f"Topic start positions: {at_top} at top of page, {mid} mid-page, {unknown} unknown."
        f"  Boundary mode: {boundary}"
    )
    if boundary == "auto" and unknown:
        print("  (Topics with unknown position include the shared page, so no content is lost.)")

    sections = build_sections(toc, total_pages, top_level_only, boundary, top_margin)
    if not sections:
        print("No sections to extract after filtering.", file=sys.stderr)
        return -1

    if select:
        try:
            chosen = parse_selection(select, len(sections))
        except ValueError as e:
            print(f"Error in --select: {e}", file=sys.stderr)
            return -1
        sections_to_write = [(n, sections[n - 1]) for n in chosen]
        print(f"Selected {len(sections_to_write)} of {len(sections)} section(s).")
    else:
        sections_to_write = [(i + 1, s) for i, s in enumerate(sections)]

    # Zero-padded numbers sized to the section count so files sort correctly
    width = max(3, len(str(len(sections))))
    multi = len(sections) > 1

    def name_for(num: int, s: dict) -> str:
        number_prefix = f"{num:0{width}d}_" if multi else ""
        return f"{prefix}{number_prefix}{sanitize_filename(s['title'])}.pdf"

    is_merge = merge_selected and bool(select) and len(sections_to_write) > 1
    print(f"Ghostscript: {'on (' + GS_EXE + ')' if use_gs else 'off'}   Workers: {workers}")

    run_info = {
        "script_version": SCRIPT_VERSION,
        "source": input_path.name,
        "source_size": original_size,
        "top_level_only": top_level_only,
        "boundary": boundary,
        "top_margin": top_margin,
        "prefix": prefix,
        "ghostscript": use_gs,
    }

    # Build the list of jobs
    if is_merge:
        titles = "_".join(sanitize_filename(s["title"])[:30] for _, s in sections_to_write)
        jobs = [{
            "label": f"merge of {len(sections_to_write)} sections",
            "ranges": [(s["start"], s["end"]) for _, s in sections_to_write],
            "filename": f"{prefix}{titles}.pdf",
        }]
    else:
        jobs = [{
            "label": f"[{num:{width}d}/{len(sections)}]",
            "ranges": [(s["start"], s["end"])],
            "filename": name_for(num, s),
        } for num, s in sections_to_write]

    for j in jobs:
        j["pages"] = sum(e - s + 1 for s, e in j["ranges"])

    if dry_run:
        skip = plan_resume([j["filename"] for j in jobs], [j["pages"] for j in jobs],
                           output_dir, force, verify_all, verify_newest=0)
        for i, j in enumerate(jobs):
            if i in skip:
                continue
            rng = ", ".join(f"{s+1}-{e+1}" for s, e in j["ranges"])
            print(f"  {j['label']} pages {rng} ({j['pages']} pg): {j['filename']}")
        print(f"\nDry run complete. {len(jobs) - len(skip)} file(s) would be written to: {output_dir}")
        return len(jobs)

    if not check_run_info(output_dir, run_info, force):
        return -1

    removed = cleanup_partials(output_dir)
    if removed:
        print(f"Removed {removed} incomplete .part file(s) from a previous run.")

    skip = plan_resume([j["filename"] for j in jobs], [j["pages"] for j in jobs],
                       output_dir, force, verify_all, verify_newest=max(4, workers * 2))

    todo = [(i, j["ranges"], str(output_dir / j["filename"])) for i, j in enumerate(jobs) if i not in skip]
    if not todo:
        print(f"\nNothing to do. All {len(jobs)} file(s) already exist in: {output_dir}")
        return len(jobs)

    print(f"Writing {len(todo)} file(s) to: {output_dir}\n")
    written = 0
    failed = []
    oversized = []
    gs_fail_count = 0
    t_start = time.time()

    def report(result):
        nonlocal written, gs_fail_count
        key, out_path_str, size, err, gs_failed = result
        j = jobs[key]
        name = Path(out_path_str).name
        if err:
            failed.append((name, err))
            print(f"  {j['label']} FAILED {name}: {err}", file=sys.stderr)
            return
        written += 1
        gs_fail_count += int(gs_failed)
        if size > original_size:
            oversized.append(name)
        elapsed = time.time() - t_start
        eta = elapsed / written * (len(todo) - written)
        print(
            f"  {j['label']} {j['pages']:4d} pg  {size/1_048_576:6.2f} MB  {name}"
            f"   ({written}/{len(todo)}, ETA {_fmt_time(eta)})"
        )

    try:
        if workers <= 1 or len(todo) == 1:
            _init_worker(str(input_path), use_gs)
            for task in todo:
                report(_write_task(task))
        else:
            with ProcessPoolExecutor(
                max_workers=workers,
                initializer=_init_worker,
                initargs=(str(input_path), use_gs),
            ) as pool:
                futures = [pool.submit(_write_task, t) for t in todo]
                try:
                    for fut in as_completed(futures):
                        report(fut.result())
                except KeyboardInterrupt:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
    except KeyboardInterrupt:
        print("\nStopped by user. Run the same command again to resume.", file=sys.stderr)
        return -1

    print(
        f"\nDone in {_fmt_time(time.time() - t_start)}. {written} file(s) written,"
        f" {len(skip)} skipped, {len(failed)} failed. Output: {output_dir}"
    )
    if gs_fail_count:
        print(f"Note: Ghostscript failed on {gs_fail_count} file(s); those were saved without font subsetting.")
    if oversized:
        print(f"WARNING: {len(oversized)} file(s) are larger than the original PDF.")
    if failed:
        print("Some sections failed. Run the same command again to retry them.", file=sys.stderr)
        return -1

    return written + len(skip)


def main():
    default_workers = max(1, min(4, (os.cpu_count() or 2) - 1))

    parser = argparse.ArgumentParser(
        description="Split a PDF into sections based on its Table of Contents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  python pdf_splitter.py report.pdf
  python pdf_splitter.py report.pdf -o ./chapters --workers 6
  python pdf_splitter.py report.pdf --top-level-only
  python pdf_splitter.py report.pdf --list-toc
  python pdf_splitter.py report.pdf --top-level-only --select 2,4
  python pdf_splitter.py report.pdf --top-level-only --select 1-3,5 --merge
  python pdf_splitter.py report.pdf --dry-run
  python pdf_splitter.py report.pdf --no-gs          (much faster, larger files)

Re-running the same command after a crash or Ctrl+C resumes where it stopped.
Default workers on this machine: {default_workers}
        """,
    )
    parser.add_argument("input", help="Path to the input PDF file")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Directory for output files (default: <input_name>_split/)")
    parser.add_argument("--top-level-only", action="store_true",
                        help="Only split on top-level TOC entries (ignore sub-chapters)")
    parser.add_argument("--prefix", default="", help="Optional prefix for all output filenames")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be written without writing any files")
    parser.add_argument("--list-toc", action="store_true",
                        help="Print the TOC with page ranges and exit")
    parser.add_argument("--select", default="", metavar="NUMS",
                        help="Section numbers or ranges to extract, e.g. '2,4' or '1-3,5'")
    parser.add_argument("--merge", action="store_true",
                        help="With --select, combine the chosen sections into one file")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild every section even if output files already exist")
    parser.add_argument("--verify-all", action="store_true",
                        help="On resume, open and check every existing file (slow)")
    parser.add_argument("--boundary", choices=["auto", "always", "never"], default="auto",
                        help=("How to end a section when the next topic starts on a new page. "
                              "auto (default): include that page unless the next topic starts at "
                              "the top of it. always: always include it. never: old behavior."))
    parser.add_argument("--top-margin", type=float, default=0.10,
                        help=("For --boundary auto: a topic counts as starting at the top of a page "
                              "if it begins within this fraction of the page height (default 0.10)"))
    parser.add_argument("--workers", type=int, default=default_workers,
                        help=f"Parallel worker processes (default {default_workers})")
    parser.add_argument("--no-gs", action="store_true",
                        help="Skip Ghostscript font subsetting (faster, larger files)")

    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"Error: {input_path} not found.", file=sys.stderr)
        sys.exit(1)

    if args.list_toc:
        reader = PdfReader(str(input_path))
        toc = extract_toc(reader)
        if not toc:
            print("No TOC found.")
            sys.exit(0)
        total_pages = len(reader.pages)
        sections = build_sections(toc, total_pages, args.top_level_only, args.boundary, args.top_margin)
        entries = [e for e in toc if e["depth"] == 0] if args.top_level_only else toc
        width = len(str(len(sections)))
        print(f"TOC for: {input_path.name}  ({total_pages} pages)\n")
        for i, (e, s) in enumerate(zip(entries, sections), start=1):
            pos = "  ?" if e["top"] is None else f"{e['top']*100:3.0f}"
            indent = "" if args.top_level_only else "  " * e["depth"]
            print(f"  [{i:{width}d}] p.{s['start']+1}-{s['end']+1}  (starts {pos}% down)  {indent}{e['title']}")
        sys.exit(0)

    output_dir = args.output_dir or str(input_path.parent / (input_path.stem + "_split"))

    result = split_pdf(
        input_path=str(input_path),
        output_dir=output_dir,
        top_level_only=args.top_level_only,
        select=args.select,
        merge_selected=args.merge,
        prefix=args.prefix,
        dry_run=args.dry_run,
        force=args.force,
        verify_all=args.verify_all,
        boundary=args.boundary,
        top_margin=args.top_margin,
        workers=max(1, args.workers),
        use_gs=not args.no_gs,
    )
    sys.exit(0 if result > 0 else 1)


if __name__ == "__main__":
    main()
