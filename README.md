# CHC Data Downloader

`download-chc-data` safely mirrors a selected directory or artifact from the
public CHC data service. It discovers directory-index links, filters the plan
before downloading, and publishes completed files atomically.

The downloader is generic: the selected URL determines the product and file
layout. It does not interpret dataset contents.

## Quick Start: CHIRPS v3 Daily TIFFs

This walkthrough requires Python 3.10 or newer.

### Step 0: Open the Repository

Open a terminal. On Windows, use PowerShell. If the repository is not already
cloned, run:

```bash
git clone https://github.com/chc-ucsb/chc-data-download.git
cd chc-data-download
```

If it is already cloned, open the terminal in that directory instead.

### Step 1: Install uv

Check whether `uv` is installed:

```bash
uv --version
```

If it is not installed, use the official installer. On macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

In Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

See the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)
for other installation methods.

### Step 2: Install the Project

From the repository directory, run:

```bash
uv sync
uv run download-chc-data --help
```

`uv sync` creates the project environment and installs the locked dependencies.

### Step 3: Preview a Download

This example selects daily CHIRPS v3 TIFF files from January 1 through January
10, 2026 and prints the plan without downloading artifacts.

On macOS or Linux:

```bash
uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/2026/ \
  --suffix .tif \
  --start-date 2026-01-01 \
  --end-date 2026-01-10 \
  --dest ./data/chirps-v3 \
  --dry-run
```

In Windows PowerShell, use a backtick instead of a backslash for line
continuation:

```powershell
uv run download-chc-data `
  --url https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/2026/ `
  --suffix .tif `
  --start-date 2026-01-01 `
  --end-date 2026-01-10 `
  --dest .\data\chirps-v3 `
  --dry-run
```

Review the listed URLs and planned file count before downloading. Change the
dates for another interval, or use `--start-year` and `--end-year` for whole
years.

### Step 4: Download the Files

After reviewing the plan, run the same command without `--dry-run`.

```bash
uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/2026/ \
  --suffix .tif \
  --start-date 2026-01-01 \
  --end-date 2026-01-10 \
  --dest ./data/chirps-v3
```

The PowerShell command is the same, using backticks for continuation and
`--dest .\data\chirps-v3`. Downloaded artifacts under `data/` are ignored by
Git in this repository.

## Destination

`--dest` is required. Relative paths are resolved from the directory where the
command runs; absolute paths work from anywhere. Directory downloads mirror the
selected URL subtree below the destination, while direct artifacts are written
directly under it.

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

## Daily Forecast Update

To preview the current CHIRPS-GEFS v3 daily run, use the current date for both
date filters:

In Windows PowerShell:

```powershell
$today = Get-Date -Format "yyyy-MM-dd"
uv run download-chc-data `
  --url https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/ `
  --pattern "c3g_*" `
  --exclude created `
  --start-date $today `
  --end-date $today `
  --dest .\data\chirps-gefs-v3 `
  --dry-run
```

On macOS or Linux:

```bash
today=$(date +%F)
uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/ \
  --pattern 'c3g_*' \
  --exclude created \
  --start-date "$today" \
  --end-date "$today" \
  --dest ./data/chirps-gefs-v3 \
  --dry-run
```

Review the plan, then remove `--dry-run` to download the run. The `--exclude`
option omits the named branch when it exists in the selected product.

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
