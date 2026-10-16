import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from download_chc_data import (
    CURRENT_UMASK,
    ByteLimitError,
    DateSelector,
    Downloader,
    DownloadError,
    PlanEntry,
    ServerRefusedError,
    extract_intervals,
    format_bytes,
    infer_cadence,
    parse_args,
    parse_index,
    validate_target,
)


class FakeResponse:
    def __init__(
        self, body=b"", *, content_type="application/octet-stream", size=None, url=None,
        status=200, headers=None,
    ):
        self.body = body
        self.headers = {"Content-Type": content_type, **(headers or {})}
        if size is not None:
            self.headers["Content-Length"] = str(size)
        self.status_code = status
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.encoding = None
        self.url = url or "https://data.chc.ucsb.edu/products/CHIRP-v3.0/"
        self.text = body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=1024):
        yield self.body

    def raise_for_status(self):
        return None

    def close(self):
        return None


class UnreadableBodyResponse(FakeResponse):
    """Fails if planning tries to read a whole artifact body via .text."""

    @property
    def text(self):
        raise AssertionError("artifact body was read during planning")

    @text.setter
    def text(self, value):
        pass


class DroppedResponse(FakeResponse):
    """Delivers part of the body, then loses the connection."""

    def __init__(self, body, keep, **kwargs):
        super().__init__(body, **kwargs)
        self.keep = keep

    def iter_content(self, chunk_size=1024):
        yield self.body[: self.keep]
        raise requests.exceptions.ChunkedEncodingError("connection broken")


class InterruptedResponse(FakeResponse):
    def iter_content(self, chunk_size=1024):
        yield self.body
        raise KeyboardInterrupt


class FakeDownloader(Downloader):
    def __init__(self, args, responses):
        super().__init__(args)
        self.responses = responses
        self.requests = []

    def _get(self, url, *, stream=False, method="GET", headers=None):
        self.requests.append((method, url, headers))
        response = self.responses[url]
        # A list scripts successive GETs; HEAD reuses the first response's headers.
        if isinstance(response, list):
            return response[0] if method == "HEAD" else response.pop(0)
        return response


class DownloaderTests(unittest.TestCase):
    def args(self, url, dest):
        return parse_args(["--url", url, "--dest", str(dest), "--delay", "0", "--jitter", "0"])

    def test_index_parser_collects_links_and_sizes(self):
        html = """
        <table>
          <tr><td><a href="daily/">daily/</a></td><td>-</td></tr>
          <tr><td><a href="rain.nc">rain.nc</a></td><td>2.0 MiB</td></tr>
          <tr><td><a href="?C=N;O=A">sort</a></td><td>-</td></tr>
        </table>
        """
        entries = parse_index(html, "https://data.chc.ucsb.edu/products/CHIRP-v3.0/")
        self.assertEqual([entry.url for entry in entries], [
            "https://data.chc.ucsb.edu/products/CHIRP-v3.0/daily/",
            "https://data.chc.ucsb.edu/products/CHIRP-v3.0/rain.nc",
        ])
        self.assertEqual(entries[1].size, 2 * 1024 * 1024)

    def test_format_bytes_uses_readable_units(self):
        self.assertEqual(format_bytes(999), "999 bytes")
        self.assertEqual(format_bytes(1024**2), "1.0 MiB")
        self.assertEqual(format_bytes(1024**3), "1.0 GiB")

    def test_url_policy_rejects_unsafe_targets(self):
        with self.assertRaises(DownloadError):
            validate_target("http://data.chc.ucsb.edu/products/CHIRP-v3.0/")
        with self.assertRaises(DownloadError):
            validate_target("https://data.chc.ucsb.edu:443/products/CHIRP-v3.0/")
        with self.assertRaises(DownloadError):
            validate_target("https://data.chc.ucsb.edu/products/EWX/")
        with self.assertRaises(DownloadError):
            validate_target("https://data.chc.ucsb.edu/products/CHIRP-v3.0/%2e%2e/secret/")
        self.assertTrue(validate_target("https://data.chc.ucsb.edu/products/EWX/CHIRPS/").endswith("/CHIRPS/"))

    def test_date_selection_handles_year_and_period_tokens(self):
        self.assertTrue(extract_intervals("chirps-v2.0.1981.01.1.tif", None))
        self.assertTrue(DateSelector(start=date(1981, 1, 1), end=date(1981, 12, 31)).matches("1981/file.tif", "annual"))
        self.assertFalse(DateSelector(start=date(2020, 1, 1)).matches("1981/file.tif", "annual"))
        self.assertEqual(infer_cadence("products/CHIRP-v3.0/pentads/africa/"), "pentad")
        self.assertEqual(extract_intervals("1981.01.2.tif", "dekad")[0], (date(1981, 1, 11), date(1981, 1, 20)))
        self.assertEqual(extract_intervals("1981.01.2.tif", "pentad")[0], (date(1981, 1, 6), date(1981, 1, 10)))
        self.assertFalse(DateSelector(start=date(2024, 2, 1), end=date(2024, 2, 29)).matches("2024.01.31.tif", "daily"))
        self.assertFalse(DateSelector(start=date(2024, 2, 1)).matches("README.txt", "daily"))
        self.assertTrue(DateSelector(start=date(2024, 2, 1), include_undated=True).matches("README.txt", "daily"))

    def test_atomic_transfer_and_size_skip(self):
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary)
            url = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/rain.nc"
            args = self.args("https://data.chc.ucsb.edu/products/CHIRP-v3.0/", dest)
            downloader = FakeDownloader(args, {url: FakeResponse(b"rain", size=4, url=url)})
            item = SimpleNamespace(url=url, size=4)
            downloader.download_file(item)
            self.assertEqual((dest / "rain.nc").read_bytes(), b"rain")
            self.assertEqual((dest / "rain.nc").stat().st_mode & 0o777, 0o666 & ~CURRENT_UMASK)
            downloader.download_file(item)
            self.assertEqual(downloader.skipped, 1)
            self.assertEqual(list(dest.glob("*.part")), [])

    def test_mismatched_transfer_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary)
            url = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/rain.nc"
            args = self.args("https://data.chc.ucsb.edu/products/CHIRP-v3.0/", dest)
            downloader = FakeDownloader(args, {url: FakeResponse(b"short", size=10, url=url)})
            with self.assertRaises(DownloadError):
                downloader.download_file(SimpleNamespace(url=url, size=10))
            self.assertFalse((dest / "rain.nc").exists())
            self.assertEqual(list(dest.glob("*.part")), [])

    def test_plan_filters_years_before_transfer(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/"
        html = """
        <table>
          <tr><td><a href="1981/">1981/</a></td><td>-</td></tr>
          <tr><td><a href="2024/">2024/</a></td><td>-</td></tr>
        </table>
        """
        with tempfile.TemporaryDirectory() as temporary:
            args = parse_args([
                "--url", base, "--dest", temporary, "--start-year", "2024",
                "--delay", "0", "--jitter", "0",
            ])
            responses = {
                base: FakeResponse(html.encode(), content_type="text/html", url=base),
                base + "2024/": FakeResponse(
                    b'<table><tr><td><a href="day.tif">day.tif</a></td><td>1 B</td></tr></table>',
                    content_type="text/html", url=base + "2024/",
                ),
            }
            downloader = FakeDownloader(args, responses)
            plan = downloader.build_plan()
            self.assertEqual([item.url for item in plan], [base + "2024/day.tif"])

    def test_plan_traverses_undated_structural_directories(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/daily/"
        branch = base + "byYear/"
        file_url = branch + "chirp-v3.0.sat.2024.days_p05.tif"
        html = '<table><tr><td><a href="byYear/">byYear/</a></td><td>-</td></tr></table>'
        file_html = (
            '<table><tr><td><a href="chirp-v3.0.sat.2024.days_p05.tif">'
            'chirp-v3.0.sat.2024.days_p05.tif</a></td><td>1 B</td></tr></table>'
        )
        with tempfile.TemporaryDirectory() as temporary:
            args = parse_args([
                "--url", base, "--dest", temporary, "--suffix", ".tif",
                "--start-year", "2024", "--delay", "0", "--jitter", "0",
            ])
            responses = {
                base: FakeResponse(html.encode(), content_type="text/html", url=base),
                branch: FakeResponse(file_html.encode(), content_type="text/html", url=branch),
            }
            downloader = FakeDownloader(args, responses)
            plan = downloader.build_plan()
            self.assertEqual([item.url for item in plan], [file_url])

    def test_parent_plan_includes_created_branch(self):
        base = "https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/"
        year = base + "2026/"
        created = base + "created/"
        html = (
            '<table><tr><td><a href="2026/">2026/</a></td><td>-</td></tr>'
            '<tr><td><a href="created/">created/</a></td><td>-</td></tr></table>'
        )
        year_html = (
            '<table><tr><td><a href="day.tif">day.tif</a></td>'
            '<td>1 B</td></tr></table>'
        )
        created_html = (
            '<table><tr><td><a href="2026/">2026/</a></td><td>-</td></tr></table>'
        )
        created_year = created + "2026/"
        created_year_html = (
            '<table><tr><td><a href="data.tif">data.tif</a></td>'
            '<td>1 B</td></tr></table>'
        )
        with tempfile.TemporaryDirectory() as temporary:
            args = parse_args([
                "--url", base, "--dest", temporary, "--suffix", ".tif",
                "--start-year", "2026", "--delay", "0", "--jitter", "0",
            ])
            responses = {
                base: FakeResponse(html.encode(), content_type="text/html", url=base),
                year: FakeResponse(year_html.encode(), content_type="text/html", url=year),
                created: FakeResponse(created_html.encode(), content_type="text/html", url=created),
                created_year: FakeResponse(
                    created_year_html.encode(), content_type="text/html", url=created_year
                ),
            }
            downloader = FakeDownloader(args, responses)
            plan = downloader.build_plan()
            self.assertCountEqual(
                [item.url for item in plan],
                [year + "day.tif", created_year + "data.tif"],
            )

    def test_exclude_path_skips_relative_branch(self):
        base = "https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/"
        year = base + "2026/"
        html = (
            '<table><tr><td><a href="2026/">2026/</a></td><td>-</td></tr>'
            '<tr><td><a href="created/">created/</a></td><td>-</td></tr></table>'
        )
        year_html = (
            '<table><tr><td><a href="day.tif">day.tif</a></td>'
            '<td>1 B</td></tr></table>'
        )
        with tempfile.TemporaryDirectory() as temporary:
            args = parse_args([
                "--url", base, "--dest", temporary, "--suffix", ".tif",
                "--start-year", "2026", "--exclude", "created",
                "--delay", "0", "--jitter", "0",
            ])
            responses = {
                base: FakeResponse(html.encode(), content_type="text/html", url=base),
                year: FakeResponse(year_html.encode(), content_type="text/html", url=year),
            }
            downloader = FakeDownloader(args, responses)
            plan = downloader.build_plan()
            self.assertEqual([item.url for item in plan], [year + "day.tif"])

    def test_explicit_created_branch_remains_downloadable(self):
        base = "https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/created/"
        year = base + "2026/"
        html = '<table><tr><td><a href="2026/">2026/</a></td><td>-</td></tr></table>'
        year_html = (
            '<table><tr><td><a href="data.tif">data.tif</a></td>'
            '<td>1 B</td></tr></table>'
        )
        with tempfile.TemporaryDirectory() as temporary:
            args = parse_args([
                "--url", base, "--dest", temporary, "--suffix", ".tif",
                "--start-year", "2026", "--delay", "0", "--jitter", "0",
            ])
            responses = {
                base: FakeResponse(html.encode(), content_type="text/html", url=base),
                year: FakeResponse(year_html.encode(), content_type="text/html", url=year),
            }
            downloader = FakeDownloader(args, responses)
            plan = downloader.build_plan()
            self.assertEqual([item.url for item in plan], [year + "data.tif"])

    def test_dated_directories_preserve_repeated_filenames(self):
        base = "https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/"
        first = base + "2026/08/01/c3g_2026.08.02.tif"
        second = base + "2026/08/02/c3g_2026.08.02.tif"
        with tempfile.TemporaryDirectory() as temporary:
            downloader = FakeDownloader(self.args(base, temporary), {})
            first_path = downloader.local_path(first)
            second_path = downloader.local_path(second)
            self.assertEqual(first_path, Path(temporary) / "2026/08/01/c3g_2026.08.02.tif")
            self.assertEqual(second_path, Path(temporary) / "2026/08/02/c3g_2026.08.02.tif")
            self.assertNotEqual(first_path, second_path)

    def test_leaf_url_is_relative_to_selected_directory(self):
        base = "https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/2026/08/01/"
        url = base + "c3g_2026.08.02.tif"
        with tempfile.TemporaryDirectory() as temporary:
            downloader = FakeDownloader(self.args(base, temporary), {})
            self.assertEqual(
                downloader.local_path(url),
                Path(temporary) / "c3g_2026.08.02.tif",
            )

    def test_suffix_filter_is_applied_during_planning(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/daily/global/"
        html = """
        <table>
          <tr><td><a href="rain.tif">rain.tif</a></td><td>1 B</td></tr>
          <tr><td><a href="rain.nc">rain.nc</a></td><td>1 B</td></tr>
        </table>
        """
        with tempfile.TemporaryDirectory() as temporary:
            args = parse_args(["--url", base, "--dest", temporary, "--suffix", ".nc", "--delay", "0", "--jitter", "0"])
            downloader = FakeDownloader(args, {base: FakeResponse(html.encode(), content_type="text/html", url=base)})
            plan = downloader.build_plan()
            self.assertEqual([item.url for item in plan], [base + "rain.nc"])

    def test_filename_pattern_filters_basename_and_composes_with_suffix(self):
        base = "https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/"
        branch = base + "c3g_2026/"
        html = (
            '<table><tr><td><a href="c3g_2026.01.01.nc">c3g_2026.01.01.nc</a></td>'
            '<td>1 B</td></tr>'
            '<tr><td><a href="rain.nc">rain.nc</a></td><td>1 B</td></tr>'
            '<tr><td><a href="other.tif">other.tif</a></td><td>1 B</td></tr>'
            '<tr><td><a href="c3g_2026/">c3g_2026/</a></td><td>-</td></tr></table>'
        )
        branch_html = '<table><tr><td><a href="other.nc">other.nc</a></td><td>1 B</td></tr></table>'
        with tempfile.TemporaryDirectory() as temporary:
            args = parse_args([
                "--url", base, "--dest", temporary, "--suffix", ".nc",
                "--pattern", "c3g_*", "--pattern", "rain*.nc",
                "--delay", "0", "--jitter", "0",
            ])
            responses = {
                base: FakeResponse(html.encode(), content_type="text/html", url=base),
                branch: FakeResponse(branch_html.encode(), content_type="text/html", url=branch),
            }
            downloader = FakeDownloader(args, responses)
            plan = downloader.build_plan()
            self.assertCountEqual(
                [item.url for item in plan],
                [base + "c3g_2026.01.01.nc", base + "rain.nc"],
            )

    def test_large_plan_requires_explicit_yes(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.args("https://data.chc.ucsb.edu/products/CHIRP-v3.0/", temporary)
            args.yes = False
            downloader = FakeDownloader(args, {})
            plan = [SimpleNamespace(url=f"https://data.chc.ucsb.edu/products/CHIRP-v3.0/{n}.tif", size=1) for n in range(1001)]
            with self.assertRaises(DownloadError):
                downloader.confirm(plan)
            args.yes = True
            self.assertTrue(downloader.confirm(plan))

    def test_dry_run_never_confirms_for_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.args("https://data.chc.ucsb.edu/products/CHIRP-v3.0/", temporary)
            args.dry_run = True
            downloader = FakeDownloader(args, {})
            self.assertFalse(downloader.confirm([SimpleNamespace(url="https://data.chc.ucsb.edu/products/CHIRP-v3.0/a.tif", size=1)]))

    def test_direct_artifact_is_not_read_during_planning(self):
        url = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/annual.nc"
        with tempfile.TemporaryDirectory() as temporary:
            response = UnreadableBodyResponse(b"\x89HDF" + b"\0" * 1024, url=url)
            downloader = FakeDownloader(self.args(url, temporary), {url: response})
            plan = downloader.build_plan()
            self.assertTrue(downloader.direct_file)
            self.assertEqual([item.url for item in plan], [url])

    def test_untyped_html_index_is_still_parsed(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/daily"
        html = b'<table><tr><td><a href="rain.tif">rain.tif</a></td><td>1 B</td></tr></table>'
        with tempfile.TemporaryDirectory() as temporary:
            response = FakeResponse(html, content_type="", url=base + "/")
            downloader = FakeDownloader(self.args(base, temporary), {base: response})
            plan = downloader.build_plan()
            self.assertFalse(downloader.direct_file)
            self.assertEqual([item.url for item in plan], [base + "/rain.tif"])

    def test_max_bytes_stops_the_run(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/"
        urls = [base + f"f{n}.tif" for n in range(5)]
        with tempfile.TemporaryDirectory() as temporary:
            args = parse_args([
                "--url", base, "--dest", temporary, "--max-bytes", "10", "--yes",
                "--delay", "0", "--jitter", "0",
            ])
            downloader = FakeDownloader(args, {url: FakeResponse(b"x" * 8, size=8, url=url) for url in urls})
            downloader.build_plan = lambda: [PlanEntry(url, None, url, None) for url in urls]
            with self.assertRaises(ByteLimitError):
                downloader.run()
            self.assertEqual([url for _, url, _ in downloader.requests], urls[:2])
            self.assertEqual((downloader.downloaded, downloader.failed), (1, 0))

    def test_interrupt_removes_partial_file(self):
        url = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/rain.nc"
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary)
            args = self.args("https://data.chc.ucsb.edu/products/CHIRP-v3.0/", dest)
            downloader = FakeDownloader(args, {url: InterruptedResponse(b"ra", size=4, url=url)})
            with self.assertRaises(KeyboardInterrupt):
                downloader.download_file(SimpleNamespace(url=url, size=4))
            self.assertEqual(list(dest.iterdir()), [])

    def test_current_file_is_skipped_with_head_only(self):
        url = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/rain.nc"
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary)
            (dest / "rain.nc").write_bytes(b"rain")
            downloader = FakeDownloader(
                self.args("https://data.chc.ucsb.edu/products/CHIRP-v3.0/", dest),
                {url: FakeResponse(b"rain", size=4, url=url)},
            )
            downloader.download_file(SimpleNamespace(url=url, size=4))
            self.assertEqual(downloader.skipped, 1)
            self.assertEqual([method for method, _, _ in downloader.requests], ["HEAD"])

    def test_newer_server_copy_is_redownloaded_and_mtime_recorded(self):
        url = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/rain.nc"
        server_time = "Fri, 13 Feb 2026 23:02:00 GMT"
        server_epoch = 1771023720
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary)
            local = dest / "rain.nc"
            local.write_bytes(b"old!")
            os.utime(local, (server_epoch - 3600, server_epoch - 3600))
            response = FakeResponse(b"new!", size=4, url=url, headers={"Last-Modified": server_time})
            downloader = FakeDownloader(
                self.args("https://data.chc.ucsb.edu/products/CHIRP-v3.0/", dest), {url: response}
            )
            downloader.download_file(SimpleNamespace(url=url, size=4))
            self.assertEqual(local.read_bytes(), b"new!")
            self.assertEqual(local.stat().st_mtime, server_epoch)
            # Now equal to the server time, so the next run skips it.
            downloader.download_file(SimpleNamespace(url=url, size=4))
            self.assertEqual((downloader.downloaded, downloader.skipped), (1, 1))

    def test_dropped_transfer_resumes_with_range(self):
        url = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/rain.nc"
        tag = {"ETag": '"abc"'}
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary)
            responses = {url: [
                DroppedResponse(b"0123456789", 4, size=10, url=url, headers=tag),
                FakeResponse(b"456789", size=6, url=url, status=206,
                             headers={**tag, "Content-Range": "bytes 4-9/10"}),
            ]}
            downloader = FakeDownloader(
                self.args("https://data.chc.ucsb.edu/products/CHIRP-v3.0/", dest), responses
            )
            with mock.patch("download_chc_data.time.sleep"):
                downloader.download_file(SimpleNamespace(url=url, size=10))
            self.assertEqual((dest / "rain.nc").read_bytes(), b"0123456789")
            self.assertEqual(downloader.requests[-1][2], {"Range": "bytes=4-", "If-Range": '"abc"'})

    def test_dropped_transfer_restarts_when_server_sends_full_file(self):
        url = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/rain.nc"
        tag = {"ETag": '"abc"'}
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary)
            responses = {url: [
                DroppedResponse(b"0123456789", 4, size=10, url=url, headers=tag),
                FakeResponse(b"abcdefghij", size=10, url=url, headers={"ETag": '"new"'}),
            ]}
            downloader = FakeDownloader(
                self.args("https://data.chc.ucsb.edu/products/CHIRP-v3.0/", dest), responses
            )
            with mock.patch("download_chc_data.time.sleep"):
                downloader.download_file(SimpleNamespace(url=url, size=10))
            self.assertEqual((dest / "rain.nc").read_bytes(), b"abcdefghij")

    def test_get_follows_redirects_and_honors_zero_retry_after(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/daily"
        with tempfile.TemporaryDirectory() as temporary:
            downloader = Downloader(self.args(base, temporary))
            replies = [
                FakeResponse(status=301, url=base, headers={"Location": base + "/"}),
                FakeResponse(status=503, url=base + "/", headers={"Retry-After": "0"}),
                FakeResponse(b"ok", url=base + "/"),
            ]
            with (
                mock.patch.object(downloader.session, "request", side_effect=replies) as request,
                mock.patch("download_chc_data.time.sleep") as sleep,
            ):
                response = downloader._get(base)
            self.assertEqual(response.body, b"ok")
            self.assertEqual(request.call_args.args, ("GET", base + "/"))
            self.assertIn(mock.call(0.0), sleep.call_args_list)

    def test_get_rejects_redirect_outside_subtree(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/"
        with tempfile.TemporaryDirectory() as temporary:
            downloader = Downloader(self.args(base, temporary))
            reply = FakeResponse(status=302, url=base, headers={"Location": "/products/other/"})
            with (
                mock.patch.object(downloader.session, "request", return_value=reply),
                self.assertRaises(DownloadError),
            ):
                downloader._get(base)

    def test_year_ranges_cover_their_span(self):
        selector = DateSelector(start=date(1995, 1, 1), end=date(1995, 12, 31))
        self.assertTrue(selector.matches("/products/X/climatology/1981-2010/clim.tif", None))
        self.assertTrue(selector.matches("/products/X/clim_1991_to_2020.nc", None))
        self.assertFalse(selector.matches("/products/X/clim_2001-2020.nc", None))

    def test_block_message_stops_without_retrying(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/"
        with tempfile.TemporaryDirectory() as temporary:
            downloader = Downloader(self.args(base, temporary))
            reply = FakeResponse(
                b"<h1>Automated bulk download detected. Contact CHC for archive access.</h1>",
                status=429, url=base,
            )
            with (
                mock.patch.object(downloader.session, "request", return_value=reply) as request,
                self.assertRaises(ServerRefusedError) as raised,
            ):
                downloader._get(base)
            self.assertEqual(request.call_count, 1)
            self.assertIn("Automated bulk download detected", str(raised.exception))

    def test_persistent_429_stops_after_retries(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/"
        with tempfile.TemporaryDirectory() as temporary:
            args = parse_args([
                "--url", base, "--dest", temporary, "--max-retries", "2",
                "--delay", "0", "--jitter", "0",
            ])
            downloader = Downloader(args)
            with (
                mock.patch.object(
                    downloader.session, "request",
                    side_effect=lambda *a, **k: FakeResponse(status=429, url=base),
                ) as request,
                mock.patch("download_chc_data.time.sleep"),
                self.assertRaises(ServerRefusedError),
            ):
                downloader._get(base)
            self.assertEqual(request.call_count, 3)

    def test_server_refusal_stops_the_run(self):
        base = "https://data.chc.ucsb.edu/products/CHIRP-v3.0/"
        urls = [base + f"f{n}.tif" for n in range(3)]
        with tempfile.TemporaryDirectory() as temporary:
            downloader = FakeDownloader(self.args(base, temporary), {})
            downloader.build_plan = lambda: [PlanEntry(url, None, url, None) for url in urls]
            downloader.args.yes = True
            requested = []

            def refuse(url, **kwargs):
                requested.append(url)
                raise ServerRefusedError("blocked")

            downloader._get = refuse
            with self.assertRaises(ServerRefusedError):
                downloader.run()
            self.assertEqual(requested, urls[:1])
            self.assertEqual(downloader.failed, 0)


if __name__ == "__main__":
    unittest.main()
