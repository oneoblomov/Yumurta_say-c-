"""Version helpers shared by runtime and updater."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT_DIR / "VERSION"
DEFAULT_VERSION = "1.0.0"


def normalize_version(value: str | None) -> str:
    if not value:
        return DEFAULT_VERSION
    value = str(value).strip()
    if value.lower().startswith("v"):
        value = value[1:]
    return value or DEFAULT_VERSION


def display_version(value: str | None) -> str:
    return f"v{normalize_version(value)}"


def read_version(default: str = DEFAULT_VERSION) -> str:
    if VERSION_FILE.exists():
        raw = VERSION_FILE.read_text(encoding="utf-8").strip()
        if raw:
            return normalize_version(raw)
    return normalize_version(default)


def write_version(value: str) -> None:
    VERSION_FILE.write_text(f"{normalize_version(value)}\n", encoding="utf-8")


def _version_key(value: str) -> List[Tuple[int, object]]:
    parts = re.split(r"[.+\-_]", normalize_version(value))
    key: List[Tuple[int, object]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def compare_versions(left: str, right: str) -> int:
    left_key = _version_key(left)
    right_key = _version_key(right)
    max_len = max(len(left_key), len(right_key))
    left_key.extend([(0, 0)] * (max_len - len(left_key)))
    right_key.extend([(0, 0)] * (max_len - len(right_key)))
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0