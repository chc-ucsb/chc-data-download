#!/usr/bin/env bash
# Daily CHIRPS v3 / CHIRPS3-GEFS update using the CHC download tool.
#
# Setup (once; requires Python 3.10+ and uv, https://docs.astral.sh/uv/):
#   git clone https://github.com/chc-ucsb/chc-data-download.git
#   cd chc-data-download
#   uv sync
# Set UA and DATA below, then run the script once a day (for example from
# cron) from the repository folder:
#   bash examples/update_chirps.sh
# A copy saved elsewhere in the repository folder works the same way.
#
# Products are fetched one after another, and the script stops at the first
# error. Each run downloads only files that are new or updated on the server;
# files you already have are skipped. The first run also fills in the last 60
# days. To preview a run without downloading, add --dry-run to the get() line.
#
# On macOS, replace "-d yesterday" with "-v-1d" and "-d '60 days ago'" with
# "-v-60d" in the date commands below.
set -euo pipefail

UA="your-project (you@example.org)"   # identifies your requests to CHC
DATA="$HOME/chirps"                    # where to store the files
TODAY=$(date -u +%F)
YESTERDAY=$(date -u -d yesterday +%F)
RECENT=$(date -u -d '60 days ago' +%F)
YEAR=$(date -u +%Y)
CHIRPS=https://data.chc.ucsb.edu/products/CHIRPS/v3.0

get() { uv run download-chc-data --user-agent "$UA" "$@"; }

# CHIRPS v3 preliminary pentads (GeoTIFF), recent pentads
get --url "$CHIRPS/prelim/pentads/global/tifs/" \
    --start-date "$RECENT" --end-date "$TODAY" --dest "$DATA/prelim-pentads"

# CHIRPS v3 daily, IMERG-blended (sat) and ERA5-blended (rnl), recent monthly NetCDFs
get --url "$CHIRPS/daily/final/sat/netcdf/byMonth/" --suffix .nc \
    --start-date "$RECENT" --end-date "$TODAY" --dest "$DATA/daily-sat"
get --url "$CHIRPS/daily/final/rnl/netcdf/byMonth/" --suffix .nc \
    --start-date "$RECENT" --end-date "$TODAY" --dest "$DATA/daily-rnl"

# CHIRPS v3 monthly and pentads, current-year NetCDF (re-downloaded when updated).
# For GeoTIFFs instead, use $CHIRPS/monthly/global/tifs/ and
# $CHIRPS/pentads/global/tifs/ with --start-date "$RECENT" --end-date "$TODAY".
get --url "$CHIRPS/monthly/global/netcdf/by_year/" \
    --start-year "$YEAR" --end-year "$YEAR" --dest "$DATA/monthly"
get --url "$CHIRPS/pentads/global/netcdf/" \
    --start-year "$YEAR" --end-year "$YEAR" --dest "$DATA/pentads"

# CHIRPS3-GEFS daily 16-day forecast, today's and yesterday's runs. The forecast
# is usually published around 08:25 UTC; the two-day window picks up a late run
# the next day.
get --url https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/ \
    --pattern 'c3g_*' --exclude created \
    --start-date "$YESTERDAY" --end-date "$TODAY" --dest "$DATA/chirps-gefs-v3"
