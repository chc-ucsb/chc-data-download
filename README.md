# CHC Data Downloader

`download-chc-data` mirrors a selected directory or artifact from the public
CHC data service. The URL supplies the product, version, cadence, region,
variable, and format branch. The downloader follows only advertised Apache
links, filters files before downloading, and publishes files atomically.

## Quick Start: CHIRPS v3 Daily

### Step 0: Open the repository

1. Open the repository page: <https://github.com/chc-ucsb/chc-data-download>.
2. Select **Code**, choose **HTTPS**, and select **Copy** to copy the repository URL.
3. Open a terminal. On Windows, open PowerShell; on macOS or Linux, open Terminal.
4. Clone the repository and enter its directory:

   ```bash
   git clone https://github.com/chc-ucsb/chc-data-download.git
   cd chc-data-download
   ```

If you already cloned the repository, open a terminal in that directory instead.

### Step 1: Install uv

Check whether `uv` is installed:

```bash
uv --version
```

If the command is not found, install `uv` using the official installer. On
macOS or Linux, run:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

In Windows PowerShell, run:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen the terminal after installation, then check `uv --version`
again. See the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)
for other installation methods.

### Step 2: Install the project

From the repository directory, run:

```bash
uv sync
uv run download-chc-data --help
```

`uv sync` creates the project environment and installs the locked dependencies.

### Step 3: Preview a CHIRPS v3 download

The following example selects daily CHIRPS v3 NetCDF files from January 1
through January 10, 2023, and prints the download plan without transferring
anything.

On macOS or Linux, use the Bash form:

```bash
uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/netcdf/ \
  --suffix .nc \
  --start-date 2023-01-01 \
  --end-date 2023-01-10 \
  --dest ./data/chirps3-daily \
  --dry-run
```

On Windows PowerShell, use a backtick at the end of each continued line. Do
not use a backslash; PowerShell passes it to the program as an argument:

```powershell
uv run download-chc-data `
  --url https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/netcdf/ `
  --suffix .nc `
  --start-date 2023-01-01 `
  --end-date 2023-01-10 `
  --dest ./data/chirps3-daily `
  --dry-run
```

You can also put the entire PowerShell command on one line. Review the listed
URLs and planned file count before downloading. Change the dates for a
different interval, or use `--start-year` and `--end-year` for whole years.

### Step 4: Download the files

After reviewing the plan, run the same Bash command without `--dry-run`:

```bash
uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/netcdf/ \
  --suffix .nc \
  --start-date 2023-01-01 \
  --end-date 2023-01-10 \
  --dest ./data/chirps3-daily
```

The PowerShell form is:

```powershell
uv run download-chc-data `
  --url https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/netcdf/ `
  --suffix .nc `
  --start-date 2023-01-01 `
  --end-date 2023-01-10 `
  --dest ./data/chirps3-daily
```

Files are saved under `data/chirps3-daily`. The downloader uses one delayed
connection, skips existing files when their sizes match, and writes completed
files atomically. Use `--overwrite` only when an existing file must be
replaced. The `data/` directory is ignored by Git so downloaded artifacts are
not accidentally committed.

## Install

```bash
uv sync
```

## Examples

Choose a URL at the narrowest useful directory and always choose a destination:

The multiline examples in this section use Bash syntax. In PowerShell,
replace each trailing `\` with a backtick, or paste the command on one line.

```bash
uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRP-v3.0/daily/global/ \
  --suffix .tif \
  --start-date 2020-01-01 \
  --end-date 2020-12-31 \
  --dest ./data/chirp-v3-daily
```

Filter CHIRPS NetCDF files or CHIRPS-GEFS TIFFs with the same interface:

```bash
uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/netcdf/ \
  --suffix .nc \
  --start-year 2022 --end-year 2024 \
  --dest ./data/chirps-netcdf

uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/16_day/ \
  --suffix .tif \
  --start-year 2024 --end-year 2025 \
  --dest ./data/chirps-gefs-v3
```

CHIRTS branches are selected by URL in the same way:

```bash
uv run download-chc-data \
  --url https://data.chc.ucsb.edu/products/CHIRTS-GEFS/v2/daily/ \
  --suffix .nc --dest ./data/chirts-gefs
```

Use `--dry-run` before downloading an unfamiliar or large selection:

```bash
uv run download-chc-data --url URL --dest ./data --suffix .nc --dry-run
```

## Filtering

`--suffix` is repeatable and case-insensitive. It accepts endings such as
`.nc`, `.tif`, `.bil`, `.nc.gz`, or `.zip`. Without it, all advertised files
are eligible.

Date filtering is cadence-aware from the URL path. The downloader recognizes
`daily`, `pentad(s)`, `dekad(s)`, `monthly`, `2-monthly` through `6-monthly`,
and annual/year directories. It interprets common CHC forms including:

- Daily: `YYYY.MM.DD`, `YYYY-MM-DD`, `YYYYMMDD`, and `YYYY/MM/DD`
- Pentad/dekad: `YYYY.MM.N`, with the directory cadence deciding how `N` is mapped
- Monthly and multi-month: `YYYY.MM` or `YYYY/MM`
- Annual: `YYYY`

`--start-year`/`--end-year` are inclusive convenience filters. Use
`--start-date`/`--end-date` for exact intervals. Files without recognizable
dates are excluded when a date filter is active; use `--include-undated` to
retain README and documentation files.

## Safety

The URL must begin with `https://data.chc.ucsb.edu/` and remain below
`/products/` or `/experimental/`. Credentials, query strings, fragments,
off-site links, redirects outside the selected subtree, traversal, guessed
filenames, and unsafe local path components are rejected.

The downloader uses one sequential connection with a default 0.5-second delay
plus jitter. It retries transient failures and honors `Retry-After`. Files are
streamed to exclusive temporary files, checked against `Content-Length` when
available, and atomically renamed only after completion. Existing files are
skipped when their size matches; `--overwrite` forces replacement.

Before transferring artifacts, the tool counts selected files and parses sizes
shown by the directory index. It prompts above 1,000 files or 10 GB, defaults
to no, and requires `--yes` in noninteractive use. Unknown sizes also require
confirmation. Hard limits are available with `--max-files`, `--max-bytes`, and
`--max-depth`.
