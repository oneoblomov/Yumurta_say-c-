#!/usr/bin/env python3
"""
check_cam.py - Kamera zamanlama failsafe scripti
================================================
systemd timer tarafından çağrılır. Web API'den pipeline durumunu okur,
DB'deki kamera saat penceresine göre gerekiyorsa başlatır, devam ettirir,
veya durdurur. API'ye erişemezse son çare olarak runpy.service'i yeniden başlatır.
"""

import json
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "egg_counter.db"
API_BASE = "http://127.0.0.1:8000"
DEFAULT_START = "08:00"
DEFAULT_END = "16:00"


def normalize_schedule(value: str, default: str) -> str:
    raw = str(value or default).strip()
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        parsed = datetime.strptime(default, "%H:%M").time()
    return parsed.strftime("%H:%M")


def read_schedule() -> tuple[str, str]:
    if not DB_PATH.exists():
        return DEFAULT_START, DEFAULT_END

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?)",
            ("camera_active_start", "camera_active_end"),
        ).fetchall()
    finally:
        conn.close()

    settings = {key: value for key, value in rows}
    start = normalize_schedule(settings.get("camera_active_start"), DEFAULT_START)
    end = normalize_schedule(settings.get("camera_active_end"), DEFAULT_END)
    return start, end


def is_within_schedule(start: str, end: str) -> bool:
    now = datetime.now().time().replace(second=0, microsecond=0)
    start_time = datetime.strptime(start, "%H:%M").time()
    end_time = datetime.strptime(end, "%H:%M").time()

    if start_time == end_time:
        return True
    if start_time < end_time:
        return start_time <= now < end_time
    return now >= start_time or now < end_time


def api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=10) as response:
        return json.load(response)


def api_post(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def restart_service() -> None:
    subprocess.run(["systemctl", "restart", "runpy.service"], check=True)


def main() -> int:
    start, end = read_schedule()
    should_run = is_within_schedule(start, end)

    try:
        status = api_get("/api/pipeline/status")
    except urllib.error.URLError as exc:
        print(f"API erişilemiyor, runpy.service yeniden başlatılıyor: {exc}")
        restart_service()
        return 0

    if should_run:
        if status.get("running") and not status.get("paused"):
            print("Pipeline plan dahilinde çalışıyor")
            return 0

        if status.get("running") and status.get("paused"):
            result = api_post("/api/pipeline/resume")
            print(f"Pipeline devam ettirildi: {result}")
            return 0

        result = api_post("/api/pipeline/start")
        if result.get("ok"):
            print(f"Pipeline otomatik başlatıldı: {result}")
            return 0

        print(f"Pipeline başlatılamadı, servis yeniden başlatılıyor: {result}")
        restart_service()
        return 1

    if status.get("running"):
        result = api_post("/api/pipeline/stop")
        print(f"Plan dışı saatte pipeline durduruldu: {result}")
    else:
        print("Plan dışı saat, pipeline kapalı")
    return 0


if __name__ == "__main__":
    sys.exit(main())