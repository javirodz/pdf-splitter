# PDF TOC Splitter

Split a large PDF into separate files, one per topic, using the PDF's Table of Contents (bookmarks).

Built for large technical manuals (9,000+ pages, 6,000+ topics) where each topic should be its own file, with all of its content and nothing cut off.

## Features

- Splits on every bookmark, or only on top-level chapters (`--top-level-only`)
- Keeps the full content of each topic, including the part of a page shared with the next topic
- Parallel processing with multiple worker processes
- Optional font subsetting with Ghostscript for smaller output files
- Safe resume after a crash, power loss, or Ctrl+C
- Select specific sections by number, and optionally merge them into one file
- Dry run and TOC listing to preview the split before writing anything

## Requirements

- Python 3.10 or later
- [pikepdf](https://pypi.org/project/pikepdf/) (page extraction)
- [pypdf](https://pypi.org/project/pypdf/) (reading the Table of Contents)
- [Ghostscript](https://www.ghostscript.com/) (optional, used to subset fonts and reduce file size)

Tested with Python 3.11, pikepdf 10.5, pypdf 3.17 and Ghostscript 10.02.

## Installation

```bash
git clone https://github.com/javirodz/pdf-splitter.git
cd pdf-splitter
pip install pikepdf pypdf
```

Ghostscript is optional. If it is installed and on your `PATH`, the script finds it automatically (`gswin64c`, `gswin32c` or `gs`). If it is not found, files are written without font subsetting.

## Usage

```bash
python pdf_splitter.py <input.pdf> [options]
```

By default, output goes to a folder named `<input_name>_split` next to the input file.

### Recommended workflow for a large PDF

1. Look at the TOC and the page range each section will get:

   ```bash
   python pdf_splitter.py manual.pdf --list-toc
   ```

2. Preview what will be written:

   ```bash
   python pdf_splitter.py manual.pdf --dry-run
   ```

3. Run the split:

   ```bash
   python pdf_splitter.py manual.pdf -o D:\manual_split
   ```

### Examples

```bash
# Split on every bookmark
python pdf_splitter.py report.pdf

# Custom output folder and 6 worker processes
python pdf_splitter.py report.pdf -o ./chapters --workers 6

# Split only on top-level chapters
python pdf_splitter.py report.pdf --top-level-only

# Extract sections 2 and 4
python pdf_splitter.py report.pdf --top-level-only --select 2,4

# Extract sections 1 to 3 and 5, merged into one file
python pdf_splitter.py report.pdf --top-level-only --select 1-3,5 --merge

# Add a prefix to every file name
python pdf_splitter.py report.pdf --prefix "MyDoc_"

# Skip Ghostscript (much faster, larger files)
python pdf_splitter.py report.pdf --no-gs
```

### Options

| Option | Description |
|---|---|
| `-o`, `--output-dir` | Output folder. Default: `<input_name>_split` |
| `--top-level-only` | Split only on top-level bookmarks |
| `--prefix TEXT` | Prefix added to every output file name |
| `--list-toc` | Print the TOC with page ranges and exit |
| `--dry-run` | Show what would be written without writing files |
| `--select NUMS` | Section numbers or ranges, for example `2,4` or `1-3,5` |
| `--merge` | With `--select`, combine the chosen sections into one file |
| `--workers N` | Number of parallel worker processes. Default: CPU cores minus 1, up to 4 |
| `--no-gs` | Skip Ghostscript font subsetting |
| `--boundary MODE` | How section ends are handled: `auto` (default), `always` or `never`. See below |
| `--top-margin F` | For `auto` mode, how close to the top of the page a topic must start to count as "top of page". Default: `0.10` (10% of page height) |
| `--force` | Rebuild every section, even if output files already exist |
| `--verify-all` | On resume, open and check every existing file |

## How sections are cut

Each section starts on the page where its bookmark points and ends on the page where the next bookmark starts.

In technical manuals a new topic often starts partway down a page. The top of that page still belongs to the previous topic. To avoid losing that content, the script reads the vertical position stored in each bookmark:

- **Next topic starts at the top of a page:** the current section stops on the page before.
- **Next topic starts partway down a page:** that page is included in both sections.
- **Bookmark has no position information:** the page is included, so no content is lost.

Example: topic A starts on page 7466 and topic B starts halfway down page 7467. Topic A is written as pages 7466 to 7467, and topic B starts on page 7467.

The `--boundary` option controls this:

| Mode | Behavior |
|---|---|
| `auto` | Default. Uses the bookmark position as described above |
| `always` | Always includes the next topic's first page. Never loses content, but adds a page to sections that end cleanly |
| `never` | Stops on the page before the next topic. Can lose content when topics start mid-page |

When a run starts, the script prints a summary like this:

```
Topic start positions: 2410 at top of page, 3590 mid-page, 0 unknown.
```

If most topics show as `unknown`, the PDF's bookmarks do not include a position, and `auto` behaves like `always`.

## Resuming an interrupted run

Run the exact same command again. The script lists the output folder once, skips sections that already have a file, and writes the rest.

How it stays safe:

- Each file is written as `<name>.pdf.part` and renamed to `<name>.pdf` only after it is complete and flushed to disk. A crash never leaves a broken file under its final name.
- Leftover `.part` files from an interrupted run are deleted on the next start.
- The most recently written files are opened and checked on resume. Use `--verify-all` to check every file.
- A file named `_split_info.json` in the output folder records the settings used. If the folder contains PDFs from a different run, different settings, or an older version of the script, the script stops with an error. Use a new output folder, or `--force` to rebuild in place.

Use the same options (`--prefix`, `--top-level-only`, `--boundary`, `--no-gs`) when resuming, so file names and settings match.

## Output file names

Files are named `<prefix><number>_<topic title>.pdf`, for example:

```
0001_Introduction.pdf
0002_Installing the Software.pdf
...
5707_Managing Roles and Permissions with AuthorizationManager.pdf
```

The number is zero padded to the total section count, so files sort in order in Windows Explorer and other file managers. Characters that are not allowed in file names are replaced with `_`, and titles are shortened to 80 characters.

## Performance

The source PDF is opened once per worker process. Each section is built by copying only its own pages into a new PDF, and sections are processed in parallel.

Ghostscript font subsetting is the slowest step. Use `--no-gs` when speed matters more than file size. Actual run time depends on the PDF (fonts, images, page count) and on the machine.

## Limitations

- The PDF must have bookmarks. Scanned PDFs or PDFs without an outline cannot be split with this tool.
- Topics that share a page both contain that full page. The script works at page level and does not crop content within a page.
- Section boundaries rely on the page and position stored in each bookmark. If the bookmarks in the source PDF are wrong, the split will be wrong too.
