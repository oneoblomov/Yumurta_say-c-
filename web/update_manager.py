"""GitHub Release based update manager."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .versioning import DEFAULT_VERSION, ROOT_DIR, compare_versions, normalize_version

if TYPE_CHECKING:
    from .database import Database


PACKAGE_PREFIX = "yumurta-sayici-"
DEFAULT_REPO_OWNER = "oneoblomov"
DEFAULT_REPO_NAME = "Yumurta_say-c-"
DEFAULT_CHANNEL = "stable"
STATUS_BUSY_STATES = {"checking", "downloading", "installing", "restarting"}
PERSISTENT_ROOTS = {
    ".git",
    ".venv",
    "venv",
    "data",
    "logs",
    "releases",
    "__pycache__",
    ".pytest_cache",
}


def _version_sort_key(value: str) -> List[Any]:
    parts: List[Any] = []
    for part in normalize_version(value).replace("-", ".").split("."):
        parts.append(int(part) if part.isdigit() else part)
    return parts


class UpdateError(RuntimeError):
    pass


class UpdateManager:
    def __init__(self, db: Optional["Database"] = None, root_dir: Path = ROOT_DIR):
        self.db = db
        self.root_dir = Path(root_dir)
        self.releases_dir = self.root_dir / "releases"
        self.packages_dir = self.releases_dir / "packages"
        self.backups_dir = self.releases_dir / "backups"
        self.manifest_file = self.releases_dir / "current_manifest.json"
        self.status_file = self.root_dir / "data" / "update_status.json"
        self.logs_dir = self.root_dir / "logs"
        self.version_file = self.root_dir / "VERSION"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def current_version(self) -> str:
        if self.version_file.exists():
            raw = self.version_file.read_text(encoding="utf-8").strip()
            if raw:
                return normalize_version(raw)
        return DEFAULT_VERSION

    def _write_version(self, version: str) -> None:
        self.version_file.write_text(f"{normalize_version(version)}\n", encoding="utf-8")

    def get_repo_owner(self) -> str:
        if self.db:
            return self.db.get_setting("update_repo_owner", DEFAULT_REPO_OWNER)
        return DEFAULT_REPO_OWNER

    def get_repo_name(self) -> str:
        if self.db:
            return self.db.get_setting("update_repo_name", DEFAULT_REPO_NAME)
        return DEFAULT_REPO_NAME

    def include_prerelease(self) -> bool:
        if not self.db:
            return False
        return (
            self.db.get_setting("update_include_prerelease", "0") == "1"
            or self.db.get_setting("update_channel", DEFAULT_CHANNEL) == "prerelease"
        )

    def auto_check_enabled(self) -> bool:
        if not self.db:
            return True
        return self.db.get_setting("update_auto_check", "1") == "1"

    def auto_install_enabled(self) -> bool:
        if not self.db:
            return False
        return self.db.get_setting("update_auto_install", "0") == "1"

    def restart_after_install_enabled(self) -> bool:
        if not self.db:
            return True
        return self.db.get_setting("update_restart_after_install", "1") == "1"

    def read_status(self) -> Dict[str, Any]:
        if self.status_file.exists():
            try:
                return json.loads(self.status_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {
            "state": "idle",
            "message": "",
            "current_version": self.current_version(),
            "latest_version": None,
            "target_version": None,
            "update_available": False,
            "busy": False,
            "last_check_at": None,
            "finished_at": None,
            "error": None,
        }

    def write_status(self, **updates: Any) -> Dict[str, Any]:
        status = self.read_status()
        status.update(updates)
        status["current_version"] = normalize_version(status.get("current_version") or self.current_version())
        state = status.get("state", "idle")
        status["busy"] = state in STATUS_BUSY_STATES
        self.status_file.write_text(
            json.dumps(status, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return status

    def is_busy(self) -> bool:
        return bool(self.read_status().get("busy"))

    def _request_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "yumurta-sayici-updater",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def _select_assets(self, release: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
        assets = release.get("assets") or []
        package_asset = None
        checksum_asset = None
        for asset in assets:
            name = asset.get("name", "")
            if name.startswith(PACKAGE_PREFIX) and name.endswith(".tar.gz"):
                package_asset = asset
            if name.startswith(PACKAGE_PREFIX) and name.endswith(".sha256"):
                checksum_asset = asset
        return {"package": package_asset, "checksum": checksum_asset}

    def list_releases(self, include_prerelease: Optional[bool] = None) -> List[Dict[str, Any]]:
        include_prerelease = self.include_prerelease() if include_prerelease is None else include_prerelease
        owner = self.get_repo_owner()
        repo = self.get_repo_name()
        raw_releases = self._request_json(f"https://api.github.com/repos/{owner}/{repo}/releases")
        installed = {}
        if self.db:
            installed = {normalize_version(item["version"]): item for item in self.db.get_versions()}
        current = self.current_version()
        releases: List[Dict[str, Any]] = []
        for item in raw_releases:
            if item.get("draft"):
                continue
            if item.get("prerelease") and not include_prerelease:
                continue
            version = normalize_version(item.get("tag_name") or item.get("name"))
            assets = self._select_assets(item)
            package_asset = assets["package"]
            checksum_asset = assets["checksum"]
            package_name = package_asset.get("name") if package_asset else f"{PACKAGE_PREFIX}v{version}.tar.gz"
            local_package = self.packages_dir / package_name
            releases.append({
                "version": version,
                "tag_name": item.get("tag_name") or f"v{version}",
                "name": item.get("name") or f"v{version}",
                "body": item.get("body") or "",
                "html_url": item.get("html_url"),
                "published_at": item.get("published_at"),
                "prerelease": bool(item.get("prerelease")),
                "package_url": package_asset.get("browser_download_url") if package_asset else item.get("tarball_url"),
                "checksum_url": checksum_asset.get("browser_download_url") if checksum_asset else None,
                "package_name": package_name,
                "package_available": bool(package_asset or item.get("tarball_url")),
                "installed": version in installed,
                "installed_record": installed.get(version),
                "is_current": compare_versions(version, current) == 0,
                "local_package_path": str(local_package) if local_package.exists() else None,
            })
        releases.sort(key=lambda rel: _version_sort_key(rel["version"]), reverse=True)
        return releases

    def _set_check_settings(self, result: Dict[str, Any]) -> None:
        if not self.db:
            return
        self.db.set_settings_bulk({
            "update_last_check_at": result.get("last_check_at") or "",
            "update_last_available_version": result.get("latest_version") or "",
            "update_last_check_status": "ok" if not result.get("error") else "error",
            "update_last_error": result.get("error") or "",
        })
        if result.get("update_available"):
            latest = result.get("latest_version")
            notified = self.db.get_setting("update_last_notified_version", "")
            if latest and latest != notified:
                self.db.add_alert(
                    "update_available",
                    f"Yeni sürüm hazır: v{latest}",
                    "info",
                    {"current_version": result.get("current_version"), "latest_version": latest},
                )
                self.db.set_setting("update_last_notified_version", latest)

    def check_for_updates(self, notify: bool = True, include_prerelease: Optional[bool] = None) -> Dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        self.write_status(state="checking", message="Sürümler kontrol ediliyor", started_at=started_at, error=None)
        try:
            releases = self.list_releases(include_prerelease=include_prerelease)
            latest = releases[0] if releases else None
            current = self.current_version()
            update_available = bool(latest and compare_versions(latest["version"], current) > 0)
            result = {
                "state": "completed",
                "message": "Güncelleme kontrolü tamamlandı",
                "current_version": current,
                "latest_version": latest["version"] if latest else None,
                "target_version": latest["version"] if update_available and latest else None,
                "update_available": update_available,
                "latest_release": latest,
                "last_check_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            }
            self.write_status(**result)
            if notify:
                self._set_check_settings(result)
            return result
        except Exception as exc:
            result = {
                "state": "error",
                "message": "Güncelleme kontrolü başarısız",
                "current_version": self.current_version(),
                "latest_version": None,
                "target_version": None,
                "update_available": False,
                "latest_release": None,
                "last_check_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            }
            self.write_status(**result)
            if notify and self.db:
                self.db.set_settings_bulk({
                    "update_last_check_at": started_at,
                    "update_last_check_status": "error",
                    "update_last_error": str(exc),
                })
            return result

    def get_status(self) -> Dict[str, Any]:
        status = self.read_status()
        status["current_version"] = self.current_version()
        status["installed_versions"] = self.db.get_versions() if self.db else []
        status["settings"] = {
            "owner": self.get_repo_owner(),
            "repo": self.get_repo_name(),
            "include_prerelease": self.include_prerelease(),
            "auto_install": self.auto_install_enabled(),
            "restart_after_install": self.restart_after_install_enabled(),
        }
        return status

    def _download_file(self, url: str, dest: Path) -> Path:
        request = urllib.request.Request(url, headers={"User-Agent": "yumurta-sayici-updater"})
        with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        return dest

    def _verify_checksum(self, package_path: Path, checksum_url: Optional[str]) -> None:
        if not checksum_url:
            return
        request = urllib.request.Request(checksum_url, headers={"User-Agent": "yumurta-sayici-updater"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
        expected = None
        for line in payload.splitlines():
            line = line.strip()
            if not line:
                continue
            if package_path.name in line:
                expected = line.split()[0]
                break
            if expected is None:
                expected = line.split()[0]
        if not expected:
            return
        digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
        if digest != expected:
            raise UpdateError(f"Checksum doğrulaması başarısız: {package_path.name}")

    def _select_release(self, version: Optional[str]) -> Dict[str, Any]:
        releases = self.list_releases()
        if not releases:
            raise UpdateError("GitHub üzerinde yayın bulunamadı")
        if not version:
            return releases[0]
        wanted = normalize_version(version)
        for release in releases:
            if normalize_version(release["version"]) == wanted:
                return release
        raise UpdateError(f"İstenen sürüm bulunamadı: v{wanted}")

    def _download_release_asset(self, release: Dict[str, Any]) -> Path:
        package_name = release["package_name"]
        destination = self.packages_dir / package_name
        if destination.exists():
            self._verify_checksum(destination, release.get("checksum_url"))
            return destination
        package_url = release.get("package_url")
        if not package_url:
            raise UpdateError(f"Sürüm paketi bulunamadı: v{release['version']}")
        self.write_status(state="downloading", message=f"v{release['version']} indiriliyor", target_version=release["version"])
        self._download_file(package_url, destination)
        self._verify_checksum(destination, release.get("checksum_url"))
        return destination

    def _resolve_extracted_root(self, temp_dir: Path) -> Path:
        manifest_here = temp_dir / "release_manifest.json"
        if manifest_here.exists():
            return temp_dir
        children = [child for child in temp_dir.iterdir() if child.is_dir()]
        if len(children) == 1 and (children[0] / "release_manifest.json").exists():
            return children[0]
        if len(children) == 1:
            return children[0]
        return temp_dir

    def _scan_manifest(self, source_root: Path) -> Dict[str, Any]:
        files = []
        for path in sorted(source_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source_root).as_posix()
            if relative.split("/", 1)[0] in PERSISTENT_ROOTS:
                continue
            files.append(relative)
        return {
            "version": self.current_version(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "files": files,
        }

    def _load_release_manifest(self, source_root: Path, release: Dict[str, Any]) -> Dict[str, Any]:
        manifest_path = source_root / "release_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = self._scan_manifest(source_root)
        manifest["version"] = normalize_version(manifest.get("version") or release["version"])
        manifest["files"] = sorted(set(manifest.get("files") or []))
        return manifest

    def _load_current_manifest(self) -> Dict[str, Any]:
        if self.manifest_file.exists():
            try:
                return json.loads(self.manifest_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return self._scan_manifest(self.root_dir)

    def _save_current_manifest(self, manifest: Dict[str, Any]) -> None:
        self.manifest_file.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _backup_current_install(self, current_version: str, current_manifest: Dict[str, Any]) -> Optional[Path]:
        files = current_manifest.get("files") or []
        existing_files = [self.root_dir / relative for relative in files if (self.root_dir / relative).exists()]
        if not existing_files:
            return None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = self.backups_dir / f"yumurta-sayici-backup-v{normalize_version(current_version)}-{timestamp}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for file_path in existing_files:
                archive.add(file_path, arcname=file_path.relative_to(self.root_dir).as_posix())
        return archive_path

    def _remove_stale_files(self, current_manifest: Dict[str, Any], new_manifest: Dict[str, Any]) -> None:
        old_files = set(current_manifest.get("files") or [])
        new_files = set(new_manifest.get("files") or [])
        stale_files = sorted(old_files - new_files, reverse=True)
        for relative in stale_files:
            target = self.root_dir / relative
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
        for relative in stale_files:
            parent = (self.root_dir / relative).parent
            while parent != self.root_dir and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _copy_manifest_files(self, source_root: Path, manifest: Dict[str, Any]) -> None:
        for relative in manifest.get("files") or []:
            source = source_root / relative
            target = self.root_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def sync_systemd_units(self) -> None:
        units_dir = self.root_dir / "systemd"
        if not units_dir.exists():
            return
        for pattern in ("*.service", "*.timer"):
            for unit_path in units_dir.glob(pattern):
                subprocess.run(["sudo", "cp", str(unit_path), "/etc/systemd/system/"], check=True)
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)

    def restart_services(self, services: Optional[List[str]] = None) -> None:
        services = services or ["runpy.service", "cloudflared.service", "egg-counter.service"]
        self.write_status(state="restarting", message="Servisler yeniden başlatılıyor")
        self.sync_systemd_units()
        for service in services:
            subprocess.run(["sudo", "systemctl", "restart", service], check=False)

    def install_release(
        self,
        version: Optional[str] = None,
        restart: bool = False,
        allow_downgrade: bool = False,
        source: str = "manual",
    ) -> Dict[str, Any]:
        release = self._select_release(version)
        current_version = self.current_version()
        if not allow_downgrade and compare_versions(release["version"], current_version) < 0:
            raise UpdateError("Daha eski sürüm yüklemek için rollback kullanın")

        started_at = datetime.now(timezone.utc).isoformat()
        self.write_status(
            state="installing",
            message=f"v{release['version']} kuruluyor",
            current_version=current_version,
            target_version=release["version"],
            started_at=started_at,
            error=None,
        )

        package_path = self._download_release_asset(release)
        current_manifest = self._load_current_manifest()
        backup_path = self._backup_current_install(current_version, current_manifest)

        with tempfile.TemporaryDirectory(prefix="yumurta-update-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            with tarfile.open(package_path, "r:gz") as archive:
                archive.extractall(temp_dir)
            source_root = self._resolve_extracted_root(temp_dir)
            new_manifest = self._load_release_manifest(source_root, release)
            self._remove_stale_files(current_manifest, new_manifest)
            self._copy_manifest_files(source_root, new_manifest)

        self._write_version(release["version"])
        new_manifest["version"] = release["version"]
        new_manifest["installed_at"] = datetime.now(timezone.utc).isoformat()
        new_manifest["source"] = source
        self._save_current_manifest(new_manifest)

        if self.db:
            self.db.add_version(
                release["version"],
                release.get("body") or "",
                str(backup_path) if backup_path else None,
                package_path=str(package_path),
                release_url=release.get("html_url"),
                release_published_at=release.get("published_at"),
                installed_by=source,
            )
            self.db.set_settings_bulk({
                "update_last_installed_version": release["version"],
                "update_last_error": "",
                "update_last_check_status": "ok",
            })

        self.sync_systemd_units()
        if restart:
            self.restart_services()

        result = {
            "state": "completed",
            "message": f"v{release['version']} kuruldu",
            "current_version": release["version"],
            "latest_version": release["version"],
            "target_version": release["version"],
            "update_available": False,
            "last_check_at": self.read_status().get("last_check_at"),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
            "backup_path": str(backup_path) if backup_path else None,
            "package_path": str(package_path),
        }
        self.write_status(**result)
        return result

    def rollback_to_version(self, version: str, restart: bool = False) -> Dict[str, Any]:
        return self.install_release(
            version=version,
            restart=restart,
            allow_downgrade=True,
            source="rollback",
        )

    def auto_update(self) -> Dict[str, Any]:
        if not self.auto_check_enabled():
            result = self.write_status(
                state="completed",
                message="Otomatik güncelleme kontrolü ayarlardan kapalı",
                current_version=self.current_version(),
                latest_version=self.read_status().get("latest_version"),
                target_version=None,
                update_available=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )
            return result
        check = self.check_for_updates(notify=True)
        if check.get("update_available") and self.auto_install_enabled():
            return self.install_release(
                version=check.get("latest_version"),
                restart=self.restart_after_install_enabled(),
                allow_downgrade=False,
                source="auto",
            )
        return check

    def register_current_install(
        self,
        version: Optional[str] = None,
        changelog: str = "Kurulum sonrası kayıt",
        package_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        version = normalize_version(version or self.current_version())
        self._write_version(version)
        manifest = self._load_current_manifest()
        manifest["version"] = version
        manifest["installed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["source"] = "setup"
        self._save_current_manifest(manifest)
        if self.db:
            active = self.db.get_active_version()
            if not active or normalize_version(active.get("version")) != version:
                self.db.add_version(
                    version,
                    changelog,
                    None,
                    package_path=package_path,
                    release_url=None,
                    release_published_at=None,
                    installed_by="setup",
                )
        result = {
            "state": "completed",
            "message": f"Mevcut kurulum v{version} olarak kaydedildi",
            "current_version": version,
            "latest_version": version,
            "target_version": version,
            "update_available": False,
            "last_check_at": None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }
        self.write_status(**result)
        return result