import tempfile
import unittest
from pathlib import Path

from .update_manager import UpdateManager


class FakeDB:
    def __init__(self):
        self.settings = {
            "update_repo_owner": "oneoblomov",
            "update_repo_name": "Yumurta_say-c-",
            "update_include_prerelease": "0",
            "update_channel": "stable",
            "update_auto_check": "1",
            "update_auto_install": "0",
            "update_restart_after_install": "1",
            "update_last_notified_version": "",
        }
        self.alerts = []
        self.versions = []

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value, category=None):
        self.settings[key] = value

    def set_settings_bulk(self, values):
        self.settings.update(values)

    def add_alert(self, alert_type, message, severity="info", data=None):
        self.alerts.append({
            "type": alert_type,
            "message": message,
            "severity": severity,
            "data": data or {},
        })

    def get_versions(self):
        return list(self.versions)

    def get_active_version(self):
        for version in self.versions:
            if version.get("is_active"):
                return version
        return None

    def add_version(self, version, changelog=None, backup_path=None,
                    package_path=None, release_url=None,
                    release_published_at=None, installed_by="manual"):
        for item in self.versions:
            item["is_active"] = 0
        self.versions.append({
            "version": version,
            "is_active": 1,
            "package_path": package_path,
            "release_url": release_url,
            "installed_by": installed_by,
        })


class FakeUpdateManager(UpdateManager):
    def __init__(self, releases, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._releases = releases

    def _request_json(self, url: str):
        return self._releases


class UpdateManagerTests(unittest.TestCase):
    def _sample_releases(self):
        return [
            {
                "tag_name": "v1.0.2",
                "name": "v1.0.2",
                "body": "Kararlı yayın",
                "html_url": "https://example.com/v1.0.2",
                "published_at": "2026-03-08T10:00:00Z",
                "draft": False,
                "prerelease": False,
                "tarball_url": "https://example.com/source.tar.gz",
                "assets": [
                    {
                        "name": "yumurta-sayici-v1.0.2.tar.gz",
                        "browser_download_url": "https://example.com/yumurta-sayici-v1.0.2.tar.gz",
                    },
                    {
                        "name": "yumurta-sayici-v1.0.2.tar.gz.sha256",
                        "browser_download_url": "https://example.com/yumurta-sayici-v1.0.2.tar.gz.sha256",
                    },
                ],
            },
            {
                "tag_name": "v1.0.1",
                "name": "v1.0.1",
                "body": "Önceki yayın",
                "html_url": "https://example.com/v1.0.1",
                "published_at": "2026-03-07T10:00:00Z",
                "draft": False,
                "prerelease": False,
                "tarball_url": "https://example.com/source-old.tar.gz",
                "assets": [
                    {
                        "name": "yumurta-sayici-v1.0.1.tar.gz",
                        "browser_download_url": "https://example.com/yumurta-sayici-v1.0.1.tar.gz",
                    }
                ],
            },
        ]

    def test_list_releases_marks_current_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir()
            (root / "logs").mkdir()
            (root / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            db = FakeDB()
            db.versions.append({"version": "1.0.1", "is_active": 1})
            manager = FakeUpdateManager(self._sample_releases(), db=db, root_dir=root)

            releases = manager.list_releases()

            self.assertEqual(releases[0]["version"], "1.0.2")
            self.assertTrue(releases[1]["is_current"])
            self.assertTrue(releases[1]["installed"])
            self.assertTrue(releases[0]["package_available"])

    def test_check_for_updates_creates_alert_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir()
            (root / "logs").mkdir()
            (root / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            db = FakeDB()
            manager = FakeUpdateManager(self._sample_releases(), db=db, root_dir=root)

            first = manager.check_for_updates(notify=True)
            second = manager.check_for_updates(notify=True)

            self.assertTrue(first["update_available"])
            self.assertEqual(first["latest_version"], "1.0.2")
            self.assertEqual(len(db.alerts), 1)
            self.assertTrue(second["update_available"])
            self.assertEqual(db.settings["update_last_notified_version"], "1.0.2")


if __name__ == "__main__":
    unittest.main()