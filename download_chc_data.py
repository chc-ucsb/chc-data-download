#!/usr/bin/env python3
"""Polite, safe downloader for datasets published by the CHC data server.

Usage:
    download-chc-data --url URL --dest DIRECTORY [OPTIONS]

The downloader follows only links advertised by the CHC Apache indexes. It
uses one delayed connection, validates the official host and subtree, plans a
download before transferring files, and publishes completed files atomically.
Runtime dependency: requests.
"""
from __future__ import annotations

import argparse
import calendar
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests

HOST = "data.chc.ucsb.edu"
ROOTS = ("/products/", "/experimental/")
DEFAULT_UA = (
    "Mozilla/5.0 (compatible; download-chc-data/1.0; polite recursive "
    "download; contact grit-ai@ucsb.edu)"
)
CONFIRM_FILES = 1_000
CONFIRM_BYTES = 10_000_000_000
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

class DownloadError(RuntimeError):
    """A user-facing download or validation failure."""


@dataclass(frozen=True)
class IndexEntry:
    url: str
    is_directory: bool
    size: int | None


@dataclass(frozen=True)
class PlanEntry:
    url: str
    size: int | None
    relative_path: str
    cadence: str | None


@dataclass(frozen=True)
class DateSelector:
    start: date | None = None
    end: date | None = None
    include_undated: bool = False

    def matches(self, text: str, cadence: str | None) -> bool:
        if self.start is None and self.end is None:
            return True
        intervals = extract_intervals(text, cadence)
        if not intervals:
            return self.include_undated
        for first, last in intervals:
            if not (
                self.start is not None
                and last < self.start
                or self.end is not None
                and first > self.end
            ):
                return True
        return False


class LinkParser(HTMLParser):
    """Extract Apache index links and the human-readable size in each row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[str, str]] = []
        self._row: list[str] | None = None
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
            self._href = None
        elif tag == "a" and self._row is not None:
            for name, value in attrs:
                if name.lower() == "href" and value:
                    self._href = value
                    break

    def handle_data(self, data: str) -> None:
        if self._row is not None:
            self._row.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._row is not None:
            if self._href:
                self.entries.append((self._href, " ".join(self._row)))
            self._row = None
            self._href = None


SIZE_RE = re.compile(
    r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)\b",
    re.IGNORECASE,
)
UNIT_MULTIPLIERS = {
    "b": 1,
    "kb": 1_000,
    "kib": 1024,
    "mb": 1_000_000,
    "mib": 1024**2,
    "gb": 1_000_000_000,
    "gib": 1024**3,
    "tb": 1_000_000_000_000,
    "tib": 1024**4,
}


def parse_size(row_text: str) -> int | None:
    matches = list(SIZE_RE.finditer(row_text))
    if not matches:
        return None
    match = matches[-1]
    return int(float(match.group("number")) * UNIT_MULTIPLIERS[match.group("unit").lower()])


def parse_index(html: str, page_url: str) -> list[IndexEntry]:
    parser = LinkParser()
    parser.feed(html)
    entries: list[IndexEntry] = []
    seen: set[str] = set()
    for href, row_text in parser.entries:
        if (
            not href
            or href.startswith(("?", "#", "../", "mailto:", "javascript:"))
            or href in {"/", ".."}
        ):
            continue
        full = urljoin(page_url, href)
        parts = urlsplit(full)
        if not parts.scheme or not parts.netloc:
            continue
        canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if canonical in seen:
            continue
        seen.add(canonical)
        entries.append(IndexEntry(canonical, parts.path.endswith("/"), parse_size(row_text)))
    return entries


def _decoded_path_parts(path: str) -> list[str]:
    raw_parts = path.split("/")
    decoded: list[str] = []
    for raw in raw_parts:
        part = unquote(raw)
        if part in {"", "."}:
            continue
        if (
            part == ".."
            or "/" in part
            or "\\" in part
            or "\x00" in part
            or any(ord(char) < 32 for char in part)
        ):
            raise DownloadError(f"unsafe URL path component: {raw!r}")
        decoded.append(part)
    return decoded


def validate_url(url: str, *, base_url: str | None = None) -> str:
    if not url.startswith(f"https://{HOST}/"):
        raise DownloadError(f"URL must begin with https://{HOST}/: {url}")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != HOST or parts.port not in (None, 443):
        raise DownloadError(f"only https://{HOST}/ URLs are allowed: {url}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise DownloadError(f"URL must not contain credentials, query, or fragment: {url}")
    path_parts = _decoded_path_parts(parts.path)
    normalized = "/" + "/".join(path_parts) + ("/" if parts.path.endswith("/") else "")
    if not any(normalized.startswith(root) for root in ROOTS):
        raise DownloadError(f"URL is outside the CHC products/experimental roots: {url}")
    result = urlunsplit(("https", HOST, normalized, "", ""))
    if base_url is not None and not is_within(result, base_url):
        raise DownloadError(f"link escapes the selected subtree: {url}")
    return result


def is_within(url: str, base_url: str) -> bool:
    target = urlsplit(url)
    base = urlsplit(base_url)
    if (target.scheme, target.hostname, target.port) != (base.scheme, base.hostname, base.port):
        return False
    target_path = target.path.rstrip("/") + "/"
    base_path = base.path.rstrip("/") + "/"
    return target_path == base_path or target_path.startswith(base_path)


def validate_target(url: str) -> str:
    normalized = validate_url(url)
    parts = urlsplit(normalized)
    path_parts = _decoded_path_parts(parts.path)
    if len(path_parts) < 2:
        raise DownloadError("a dataset path below products/ or experimental/ is required")
    top = path_parts[1]
    if top in {"EWX", "EWX2"} and len(path_parts) < 3:
        raise DownloadError(f"{top} is a hosting collection; specify a subdirectory URL")
    return normalized


def infer_cadence(text: str) -> str | None:
    """Infer the nearest CHC time-step directory from a URL/path."""
    components = [unquote(part).lower() for part in text.split("/") if part]
    for component in reversed(components):
        normalized = component.replace("_", "-")
        if normalized.startswith("daily"):
            return "daily"
        if "pentad" in normalized:
            return "pentad"
        if "dekad" in normalized:
            return "dekad"
        if re.fullmatch(r"[2-6]-monthly", normalized):
            return normalized
        if normalized == "monthly":
            return "monthly"
        if normalized in {"annual", "annually", "yearly", "year"}:
            return "annual"
    return None


def month_interval(year: int, month: int, cadence: str | None) -> tuple[date, date] | None:
    if not 1 <= month <= 12:
        return None
    months = 1
    if cadence and cadence.endswith("-monthly"):
        months = int(cadence.split("-", 1)[0])
    first = date(year, month, 1)
    end_month_index = year * 12 + month - 1 + months - 1
    end_year, end_month_zero = divmod(end_month_index, 12)
    end_month = end_month_zero + 1
    last = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    return first, last


def period_interval(year: int, month: int, period: int, cadence: str) -> tuple[date, date] | None:
    if not 1 <= month <= 12:
        return None
    days = calendar.monthrange(year, month)[1]
    if cadence == "pentad":
        first_day = 1 + (period - 1) * 5
        last_day = min(first_day + 4, days)
    else:
        first_day = {1: 1, 2: 11, 3: 21}.get(period, 0)
        last_day = {1: 10, 2: 20, 3: days}.get(period, 0)
    if not first_day or first_day > days:
        return None
    return date(year, month, first_day), date(year, month, last_day)


def extract_intervals(text: str, cadence: str | None) -> list[tuple[date, date]]:
    decoded = unquote(text)
    intervals: list[tuple[date, date]] = []
    seen: set[tuple[date, date]] = set()

    def add(interval: tuple[date, date] | None) -> None:
        if interval is not None and interval not in seen:
            seen.add(interval)
            intervals.append(interval)

    full_pattern = re.compile(r"(?<!\d)(\d{4})[-_. /](\d{1,2})[-_. /](\d{1,2})(?!\d)")
    for match in full_pattern.finditer(decoded):
        year, month, value = map(int, match.groups())
        if cadence in {"pentad", "dekad"}:
            add(period_interval(year, month, value, cadence))
        else:
            try:
                add((date(year, month, value), date(year, month, value)))
            except ValueError:
                pass

    compact = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")
    for match in compact.finditer(decoded):
        try:
            value = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            add((value, value))
        except ValueError:
            pass
    if intervals:
        return intervals

    month_pattern = re.compile(r"(?<!\d)(\d{4})[-_. /](\d{1,2})(?!\d)")
    for match in month_pattern.finditer(decoded):
        add(month_interval(int(match.group(1)), int(match.group(2)), cadence))
    if intervals:
        return intervals

    for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", decoded):
        value = int(match.group(1))
        add((date(value, 1, 1), date(value, 12, 31)))
    return intervals


def extract_interval(text: str, cadence: str | None = None) -> tuple[date, date] | None:
    """Return the first recognized interval for compatibility and diagnostics."""
    intervals = extract_intervals(text, cadence)
    return intervals[0] if intervals else None


def parse_date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def parse_nonnegative(value: str) -> float:
    result = float(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return result


def parse_positive_float(value: str) -> float:
    result = float(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def parse_year(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 9999:
        raise argparse.ArgumentTypeError("must be between 1 and 9999")
    return result


def parse_positive_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return result


class Downloader:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base_url = validate_target(args.url)
        self.dest = Path(args.dest).expanduser().resolve()
        self.selector = DateSelector(args.start_date, args.end_date, args.include_undated)
        self.suffixes = tuple(args.suffixes)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": args.user_agent, "Accept-Encoding": "identity"})
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.bytes_downloaded = 0
        self.direct_file = False

    def _sleep(self) -> None:
        time.sleep(self.args.delay + random.uniform(0, self.args.jitter))

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            delay = float(value)
            return delay if delay >= 0 else None
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _get(self, url: str, *, stream: bool = False) -> requests.Response:
        current = validate_url(url, base_url=self.base_url)
        for redirect_number in range(6):
            for attempt in range(1, self.args.max_retries + 2):
                self._sleep()
                try:
                    response = self.session.get(
                        current,
                        stream=stream,
                        timeout=self.args.timeout,
                        allow_redirects=False,
                    )
                except requests.RequestException as exc:
                    if attempt > self.args.max_retries:
                        raise DownloadError(f"request failed for {current}: {exc}") from exc
                    wait = min(60.0, 2.0**attempt) + random.uniform(0, self.args.jitter)
                    print(f"  retrying {type(exc).__name__} in {wait:.1f}s", file=sys.stderr)
                    time.sleep(wait)
                    continue

                if response.is_redirect:
                    location = response.headers.get("Location")
                    response.close()
                    if not location:
                        raise DownloadError(f"redirect without Location header: {current}")
                    current = validate_url(urljoin(current, location), base_url=self.base_url)
                    break
                if response.status_code in RETRY_STATUSES:
                    if attempt > self.args.max_retries:
                        response.close()
                        raise DownloadError(f"HTTP {response.status_code} for {current}")
                    wait = self._retry_after(response) or min(60.0, 2.0**attempt)
                    response.close()
                    print(f"  HTTP {response.status_code}; retrying in {wait:.1f}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    response.close()
                    raise DownloadError(f"HTTP error for {current}: {exc}") from exc
                return response
            else:
                continue
            # A validated redirect restarts the request loop at the new URL.
            continue
        raise DownloadError(f"too many redirects for {url}")

    def _directory_entries(self, url: str) -> list[IndexEntry]:
        response = self._get(url)
        try:
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower() and response.text[:100].lstrip().startswith("<"):
                content_type = "text/html"
            if "html" not in content_type.lower():
                raise DownloadError(f"expected an HTML directory index at {url}")
            return parse_index(response.text, response.url)
        finally:
            response.close()

    def build_plan(self) -> list[PlanEntry]:
        plan: list[PlanEntry] = []
        queue = [self.base_url]
        visited: set[str] = set()
        while queue:
            directory = queue.pop()
            if directory in visited:
                continue
            visited.add(directory)
            if len(directory.rstrip("/").split("/")) - len(self.base_url.rstrip("/").split("/")) > self.args.max_depth:
                continue
            try:
                entries = self._directory_entries(directory)
            except DownloadError as exc:
                if directory == self.base_url and not self.base_url.endswith("/"):
                    if not str(exc).startswith("expected an HTML directory index"):
                        raise
                    self.direct_file = True
                    relative = unquote(urlsplit(self.base_url).path)
                    cadence = infer_cadence(relative)
                    if (
                        (not self.suffixes or relative.lower().endswith(self.suffixes))
                        and self.selector.matches(relative, cadence)
                    ):
                        plan.append(PlanEntry(self.base_url, None, relative, cadence))
                    break
                raise
            for entry in entries:
                try:
                    full = validate_url(entry.url, base_url=self.base_url)
                except DownloadError:
                    continue
                relative = unquote(urlsplit(full).path)
                cadence = infer_cadence(relative)
                if not self.selector.matches(relative, cadence):
                    continue
                if entry.is_directory:
                    queue.append(full if full.endswith("/") else full + "/")
                else:
                    if self.suffixes and not relative.lower().endswith(self.suffixes):
                        continue
                    plan.append(PlanEntry(full, entry.size, relative, cadence))
                    if self.args.max_files is not None and len(plan) > self.args.max_files:
                        raise DownloadError(f"plan exceeds --max-files={self.args.max_files}")
        return plan

    def local_path(self, url: str, base_url: str | None = None) -> Path:
        base = urlsplit(base_url or self.base_url)
        target = urlsplit(url)
        base_parts = _decoded_path_parts(base.path)
        target_parts = _decoded_path_parts(target.path)
        if target_parts[: len(base_parts)] != base_parts:
            raise DownloadError(f"local path is outside selected URL subtree: {url}")
        relative = target_parts[len(base_parts) :]
        if not relative:
            relative = [target_parts[-1]]
        current = self.dest
        current.mkdir(parents=True, exist_ok=True)
        for component in relative[:-1]:
            current = current / component
            if current.is_symlink():
                raise DownloadError(f"refusing symlinked destination component: {current}")
            current.mkdir(exist_ok=True)
        path = current / relative[-1]
        if path.is_symlink():
            raise DownloadError(f"refusing symlinked destination file: {path}")
        return path

    def _expected_size(self, response: requests.Response) -> int | None:
        value = response.headers.get("Content-Length")
        return int(value) if value and value.isdigit() else None

    def download_file(self, item: PlanEntry, base_url: str | None = None) -> None:
        path = self.local_path(item.url, base_url)
        response = self._get(item.url, stream=True)
        expected = self._expected_size(response)
        if (
            expected is not None
            and path.exists()
            and path.is_file()
            and not self.args.overwrite
            and path.stat().st_size == expected
        ):
            response.close()
            self.skipped += 1
            print(f"= skip {path.relative_to(self.dest)}")
            return
        if (
            expected is not None
            and self.args.max_bytes is not None
            and self.bytes_downloaded + expected > self.args.max_bytes
        ):
            response.close()
            raise DownloadError("download would exceed --max-bytes")

        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        written = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{path.name}.", suffix=".part", dir=path.parent, delete=False
            ) as output:
                temp_name = output.name
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        written += len(chunk)
                        if self.args.max_bytes is not None and self.bytes_downloaded + written > self.args.max_bytes:
                            raise DownloadError("download exceeded --max-bytes")
                        output.write(chunk)
        except Exception:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
            raise
        finally:
            response.close()
        if expected is not None and written != expected:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
            raise DownloadError(f"size mismatch for {item.url}: got {written}, expected {expected}")
        assert temp_name is not None
        os.replace(temp_name, path)
        self.downloaded += 1
        self.bytes_downloaded += written
        print(f"+ {path.relative_to(self.dest)} ({written:,} bytes)")

    def confirm(self, plan: list[PlanEntry]) -> bool:
        known_bytes = sum(item.size or 0 for item in plan)
        unknown = sum(item.size is None for item in plan)
        print(f"Planned files: {len(plan):,}")
        print(f"Known size: {known_bytes:,} bytes" + (f" (+ {unknown:,} unknown)" if unknown else ""))
        if self.args.dry_run:
            for item in plan:
                print(item.url)
            return False
        threshold = len(plan) > CONFIRM_FILES or known_bytes > CONFIRM_BYTES or unknown > 0
        if not threshold or self.args.yes:
            return True
        message = "Download plan is large or has unknown file sizes. Continue? [y/N] "
        if not sys.stdin.isatty():
            raise DownloadError("large/unknown plan requires --yes in noninteractive use")
        return input(message).strip().lower() in {"y", "yes"}

    def run(self) -> int:
        plan = self.build_plan()
        if self.args.max_bytes is not None:
            known_bytes = sum(item.size or 0 for item in plan)
            if known_bytes > self.args.max_bytes:
                raise DownloadError("plan exceeds --max-bytes")
        if not self.confirm(plan):
            return 0
        direct_base = None
        if self.direct_file:
            direct_base = self.base_url.rsplit("/", 1)[0] + "/"
        failures: list[str] = []
        for item in plan:
            try:
                self.download_file(item, direct_base)
            except (DownloadError, OSError) as exc:
                self.failed += 1
                failures.append(f"{item.url}: {exc}")
                print(f"! {failures[-1]}", file=sys.stderr)
        print(
            f"Done: downloaded={self.downloaded} skipped={self.skipped} "
            f"failed={self.failed} bytes={self.bytes_downloaded:,}"
        )
        return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="dataset directory or artifact URL under the CHC data service")
    parser.add_argument("--dest", help="local output directory")
    parser.add_argument(
        "--suffix",
        dest="suffixes",
        action="append",
        default=[],
        help="only download files ending with this suffix; repeatable (e.g. .nc)",
    )
    parser.add_argument("--start-year", type=parse_year)
    parser.add_argument("--end-year", type=parse_year)
    parser.add_argument("--start-date", type=parse_date_arg)
    parser.add_argument("--end-date", type=parse_date_arg)
    parser.add_argument(
        "--include-undated",
        action="store_true",
        help="include files without a recognized date when a date filter is active",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="bypass the large-plan confirmation prompt")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-depth", type=parse_positive_int, default=100)
    parser.add_argument("--max-files", type=parse_positive_int)
    parser.add_argument("--max-bytes", type=parse_positive_int)
    parser.add_argument("--delay", type=parse_nonnegative, default=0.5)
    parser.add_argument("--jitter", type=parse_nonnegative, default=0.5)
    parser.add_argument("--timeout", type=parse_positive_float, default=60.0)
    parser.add_argument("--max-retries", type=parse_positive_int, default=6)
    parser.add_argument("--user-agent", default=DEFAULT_UA)
    args = parser.parse_args(argv)
    if not args.url or not args.dest:
        parser.error("--url and --dest are required")
    args.suffixes = tuple(
        suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
        for suffix in args.suffixes
    )
    if args.start_year is not None and args.start_date is not None:
        parser.error("use either --start-year or --start-date, not both")
    if args.end_year is not None and args.end_date is not None:
        parser.error("use either --end-year or --end-date, not both")
    if args.start_year is not None:
        args.start_date = date(args.start_year, 1, 1)
    if args.end_year is not None:
        args.end_date = date(args.end_year, 12, 31)
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("start date must not be after end date")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return Downloader(args).run()
    except KeyboardInterrupt:
        print("Interrupted; no incomplete file was published.", file=sys.stderr)
        return 130
    except (DownloadError, requests.RequestException, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
