#!/usr/bin/env bash
# CHIRPS v3 daily preliminary and final GeoTIFF update using the CHC download
# tool, timed to when each product is usually published.
#
# Setup (once; requires Python 3.10+ and uv, https://docs.astral.sh/uv/):
#   git clone https://github.com/chc-ucsb/chc-data-download.git
#   cd chc-data-download
#   uv sync
# Set UA and DATA below, then run the script once a day (for example from
# cron, after about 23:00 UTC) from the repository folder:
#   bash examples/update_chirps_daily.sh
# A copy saved elsewhere in the repository folder works the same way.
#
# Products are fetched one after another, and the script stops at the first
# error. Each run downloads only files that are new or updated on the server;
# files you already have are skipped. On days when no new files are expected,
# the script exits without contacting the server. To preview a run without
# downloading, add --dry-run to the get() line. For files older than the
# windows below, run a get line once with an earlier --start-date.
#
# On macOS, install GNU date (brew install coreutils) and replace "date" with
# "gdate" below; the month arithmetic for the final files needs GNU date.
set -euo pipefail

UA="your-project (you@example.org)"   # identifies your requests to CHC
DATA="$HOME/chirps"                    # where to store the files
TODAY=$(date -u +%F)
DAY=$(date -u +%-d)
CHIRPS=https://data.chc.ucsb.edu/products/CHIRPS/v3.0

get() { uv run download-chc-data --user-agent "$UA" "$@"; }

# CHIRPS v3 daily preliminary (GeoTIFF; IMERG-blended, sat only), last 15 days.
# Prelim arrives one pentad (days 1-5, 6-10, ..., 26-end) at a time, usually
# about 2 days after the pentad ends, in the afternoon UTC, so check on days
# 2-4 after each pentad ends.
case $DAY in 2|3|4|7|8|9|12|13|14|17|18|19|22|23|24|27|28|29)
  get --url "$CHIRPS/daily/prelim/sat/" --suffix .tif --exclude netcdf \
      --start-date "$(date -u -d '15 days ago' +%F)" --end-date "$TODAY" \
      --dest "$DATA/daily-prelim-sat" ;;
esac

# CHIRPS v3 daily final (GeoTIFF; IMERG-blended sat), last month. A whole
# month usually appears between the 12th and 18th of the next month, in the
# evening UTC. The excludes skip the NetCDF, 0.25-degree (p25), and COG copies
# of the same data. For the ERA5-blended version, change sat to rnl.
if (( DAY >= 12 && DAY <= 18 )); then
  MONTH1=$(date -u +%Y-%m-01)
  get --url "$CHIRPS/daily/final/sat/" --suffix .tif \
      --exclude netcdf --exclude p25 --exclude cogs \
      --start-date "$(date -u -d "$MONTH1 -1 month" +%F)" \
      --end-date "$(date -u -d "$MONTH1 -1 day" +%F)" \
      --dest "$DATA/daily-final-sat"
fi
