import unittest
from unittest import mock
from pathlib import Path

from .app import get_cloudflared_url


class CloudflaredUrlTests(unittest.TestCase):
    def test_no_log_file_returns_none(self):
        # simulate log file not existing and journal failing
        with mock.patch("pathlib.Path.exists", return_value=False), \
             mock.patch("subprocess.check_output", side_effect=Exception("fail")):
            self.assertIsNone(get_cloudflared_url())

    def test_parses_url_from_log(self):
        fake = (
            "some noise\n"
            "2026-03-09T10:32:47Z INF Your quick Tunnel has been created! Visit it at https://foo.trycloudflare.com\n"
            "more noise\n"
        )
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("builtins.open", mock.mock_open(read_data=fake)):
            url = get_cloudflared_url()
            self.assertEqual(url, "https://foo.trycloudflare.com")

    def test_parses_url_from_different_line(self):
        fake = (
            "OBLIVION\n"
            "2026-03-09T10:32:47Z INF +--------------------------------------------------------------------------------------------+\n"
            "2026-03-09T10:32:47Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |\n"
            "2026-03-09T10:32:47Z INF |  https://bar.trycloudflare.com                                            |\n"
        )
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("builtins.open", mock.mock_open(read_data=fake)):
            url = get_cloudflared_url()
            self.assertEqual(url, "https://bar.trycloudflare.com")



    def test_fallback_to_journal(self):
        fake = (
            "noise\n"
            "Your quick Tunnel has been created! Visit it at https://baz.trycloudflare.com\n"
        )
        # simulate missing/empty log file but valid journal output
        with mock.patch("pathlib.Path.exists", return_value=False), \
             mock.patch("subprocess.check_output", return_value=fake):
            url = get_cloudflared_url()
            self.assertEqual(url, "https://baz.trycloudflare.com")


if __name__ == "__main__":
    unittest.main()
