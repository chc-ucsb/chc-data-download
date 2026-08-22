# CHC Data Downloader

`download-chc-data` safely mirrors a selected directory or artifact from the
public CHC data service. It discovers directory-index links, filters the plan
before downloading, and publishes completed files atomically.

The downloader is generic: the selected URL determines the product and file
layout. It does not interpret dataset contents.

## Install

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)

Clone the repository and install its locked environment:

```bash
git clone https://github.com/chc-ucsb/chc-data-download.git
cd chc-data-download
uv sync
```

If `uv` is not installed, use its official installer. On macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

In Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Quick Start

Preview a selection before downloading it:

```bash
uv run download-chc-data \
  --url URL \
  --dest ./data \
  --suffix .tif \
  --start-date 2026-01-01 \
  --end-date 2026-01-10 \
  --dry-run
```

Replace `URL` with a directory or artifact URL under the CHC service. After
reviewing the plan, run the same command without `--dry-run`. In PowerShell,
replace Bash line-continuation backslashes with backticks, or use one line.

For example, this selects a short interval from a verified CHIRPS v3 branch:

```bash
uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/2026/ \
  --dest ./data/chirps-v3 \
  --suffix .tif \
  --start-date 2026-01-01 \
  --end-date 2026-01-10 \
  --dry-run
```

## Destination

`--dest` is required. Relative paths are resolved from the directory where the
command runs; absolute paths work from anywhere. Directory downloads mirror the
selected URL subtree below the destination, while direct artifacts are written
directly under it. Downloaded artifacts under `data/` are ignored by Git in
this repository.

## URL Selection

- URLs must use `https://data.chc.ucsb.edu/` and remain below `/products/` or
  `/experimental/`.
- A directory URL is traversed through links advertised by its directory index.
  A direct artifact URL is also supported.
- Links outside the selected subtree or allowed CHC roots are skipped.
- Structural directories without dates are traversed. Date-bearing directories
  can be pruned by date filters.
- Choose the narrowest useful URL when a service exposes multiple file
  representations of the same data.

## Filtering

### File Filters

`--suffix` is repeatable and case-insensitive. It accepts endings such as
`.nc`, `.tif`, `.bil`, `.nc.gz`, and `.zip`.

Use repeatable `--pattern` options for case-insensitive filename glob matching.
Patterns match filenames, not parent directories. Quote wildcard patterns in
the shell:

```bash
uv run download-chc-data \
  --url URL \
  --dest ./data \
  --pattern '*.tif' \
  --dry-run
```

Repeated patterns are alternatives. If both `--suffix` and `--pattern` are
provided, a file must satisfy both filters. Without either option, all
advertised files are eligible.

### Date Filters

Date filters are inferred from URL paths and filenames. The downloader
recognizes daily, pentad, dekad, monthly, multi-month, and annual/year layouts,
including common forms such as `YYYY-MM-DD`, `YYYY.MM.DD`, `YYYYMMDD`,
`YYYY/MM/DD`, `YYYY.MM`, `YYYY/MM`, and `YYYY`.

Use inclusive year filters:

```bash
uv run download-chc-data \
  --url URL \
  --dest ./data \
  --start-year 2022 \
  --end-year 2024
```

Use `--start-date` and `--end-date` for exact `YYYY-MM-DD` intervals. Do not
combine a start year with a start date, or an end year with an end date. Date
selection uses interval overlap for period-based products. Files without a
recognizable date are excluded when a date filter is active; use
`--include-undated` to retain them.

For products with nested initialization-date directories, an active date filter
uses the directory date as the initialization date. Dates in filenames do not
select a different initialization run.

## Exclude

`--exclude` removes a relative path sequence below the selected URL. It is
case-insensitive and repeatable:

```bash
uv run download-chc-data \
  --url URL \
  --dest ./data \
  --exclude unwanted-branch \
  --exclude another-branch
```

## Safety And Operations

- `--dry-run` builds and prints the plan without downloading artifacts. It still
  requests directory indexes needed for discovery.
- The default delay is 0.5 seconds plus up to 0.5 seconds of jitter. Requests
  are made sequentially, and transient request failures are retried.
- Files stream to temporary files and are atomically renamed only after
  completion. When the server supplies a usable `Content-Length`, a matching
  existing file is skipped unless `--overwrite` is used. Files with a different
  size, or files whose server size is unknown, are downloaded again.
- Download validation is size-based when `Content-Length` is available; no
  checksum verification is performed.
- Plans larger than 1,000 files, larger than 10 GB of known data, or containing
  unknown sizes require confirmation. Use `--yes` for noninteractive runs.
- Use `--max-files`, `--max-bytes`, and `--max-depth` to limit a plan. The
  default maximum depth is 100.
- Per-file failures do not stop the remaining downloads. A successful run with
  no files, including an unpublished date selection, exits normally. The
  process exits with status 1 for per-file failures, 2 for a top-level error,
  and 130 when interrupted.

Run `uv run download-chc-data --help` for all options, including timeout,
retry, delay, jitter, and user-agent settings.

## Development

Run the checks from the repository root:

```bash
uv run ruff check .
uv run python -m unittest discover -s tests -v
```
