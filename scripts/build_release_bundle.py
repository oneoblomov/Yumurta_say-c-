#!/usr/bin/env python3
"""Build a release tarball and manifest for GitHub Releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
EXCLUDED_ROOTS = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "data",
    "logs",
    "releases",
    "__pycache__",
    ".pytest_cache",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db-shm", ".db-wal"}


def normalize_version(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("v"):
        value = value[1:]
    return value


def should_include(path: Path) -> bool:
    relative = path.relative_to(ROOT_DIR)
    first = relative.parts[0]
    if first in EXCLUDED_ROOTS:
        return False
    if any(part == "__pycache__" for part in relative.parts):
        return False
    if path.suffix in EXCLUDED_SUFFIXES:
        return False
    return True


def build_manifest(version: str) -> dict:
    files = []
    for path in sorted(ROOT_DIR.rglob("*")):
        if not path.is_file():
            continue
        if not should_include(path):
            continue
        files.append(path.relative_to(ROOT_DIR).as_posix())
    return {
        "version": normalize_version(version),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build release bundle")
    parser.add_argument("--version", required=True, help="Release tag or version")
    parser.add_argument("--output-dir", required=True, help="Artifact output directory")
    args = parser.parse_args()

    version = normalize_version(args.version)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(version)
    package_name = f"yumurta-sayici-v{version}.tar.gz"
    package_path = output_dir / package_name
    checksum_path = output_dir / f"{package_name}.sha256"

    with tempfile.TemporaryDirectory(prefix="yumurta-bundle-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        bundle_root = temp_dir / "yumurta-sayici"
        bundle_root.mkdir(parents=True, exist_ok=True)

        (bundle_root / "release_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        for relative in manifest["files"]:
            source = ROOT_DIR / relative
            target = bundle_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

        with tarfile.open(package_path, "w:gz") as archive:
            archive.add(bundle_root, arcname="yumurta-sayici")

    digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{digest}  {package_name}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())