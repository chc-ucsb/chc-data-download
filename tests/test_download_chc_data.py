import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from download_chc_data import (
    DateSelector,
    Downloader,
    DownloadError,
    extract_interval,
    extract_intervals,
    infer_cadence,
    parse_args,
    parse_index,
    validate_target,
)


class FakeResponse:
    def __init__(self, body=b"", *, content_type="application/octet-stream", size=None, url=None):
        self.body = body
        self.headers = {"Content-Type": content_type}
        if size is not None:
            self.headers["Content-Length"] = str(size)
        self.status_code = 200
        self.url = url or "https://data.chc.ucsb.edu/products/CHIRP-v3.0/"
        self.text = body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=1024):
        yield self.body

    def raise_for_status(self):
        return None

    def close(self):
        return None


class FakeDownloader(Downloader):
    def __init__(self, args, responses):
        super().__init__(args)
        self.responses = responses

    def _get(self, url, *, stream=False):
        return self.responses[url]


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
        interval = extract_interval("chirps-v2.0.1981.01.1.tif")
        self.assertIsNotNone(interval)
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

    def test_leaf_url_preserves_date_directory_context(self):
        base = "https://data.chc.ucsb.edu/products/CHIRPS-GEFS/v3/daily/global/2026/08/01/"
        url = base + "c3g_2026.08.02.tif"
        with tempfile.TemporaryDirectory() as temporary:
            downloader = FakeDownloader(self.args(base, temporary), {})
            self.assertEqual(
                downloader.local_path(url),
                Path(temporary) / "2026/08/01/c3g_2026.08.02.tif",
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


if __name__ == "__main__":
    unittest.main()
