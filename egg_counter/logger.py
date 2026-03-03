"""
logger.py - Thread-Safe Log Sistemi
=====================================
Düzeltmeler:
  1. Deadlock düzeltmesi: _handle_day_change artık lock almıyor
     (zaten log_count_event'ten lock altında çağrılıyor).
  2. _total_count lock içinde güncelleniyor (eski: dışında -> race condition).
  3. flush_interval=1: Her sayımda disk'e yazılıyor (endüstriyel güvenlik).
  4. Otomatik recovery: Dosya açılamazsa bir sonraki denemede tekrar dene.
"""

import csv
import threading
from datetime import datetime, date
from pathlib import Path
from typing import List

from .config import LoggerConfig


class CountLogger:
    """Thread-safe endüstriyel log sistemi."""

    def __init__(self, config: LoggerConfig):
        self.cfg = config
        self._lock = threading.Lock()
        self._buffer: List[dict] = []
        self._total_count: int = 0
        self._current_date: date = date.today()
        self._closed = False

        self.log_dir = Path(config.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._update_file_paths()

        if self.cfg.enable_csv_log:
            self._ensure_csv_header()

    def _update_file_paths(self):
        date_str = self._current_date.isoformat()
        self.csv_path = self.log_dir / f"{self.cfg.csv_prefix}_{date_str}.csv"
        self.daily_path = self.log_dir / f"{self.cfg.daily_prefix}_{date_str}.txt"

    def _ensure_csv_header(self):
        if not self.csv_path.exists():
            try:
                with open(self.csv_path, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "timestamp", "track_id", "cx", "cy",
                        "x1", "y1", "x2", "y2", "confidence", "total"
                    ])
            except IOError as e:
                print(f"[LOGGER] CSV başlık yazma hatası: {e}")

    def log_count_event(self, event: dict):
        """
        Sayım olayını logla.
        Düzeltme: _total_count lock İÇİNDE güncelleniyor.
        Düzeltme: _handle_day_change lock ALMADAN çağrılıyor (deadlock fix).
        """
        with self._lock:
            # Gün değişimi kontrolü
            today = date.today()
            if today != self._current_date:
                self._flush_unlocked()
                self._current_date = today
                self._update_file_paths()
                self._total_count = 0
                if self.cfg.enable_csv_log:
                    self._ensure_csv_header()
                print(f"[LOGGER] Yeni gün: {today.isoformat()}")

            self._total_count = event.get("total", self._total_count)
            self._buffer.append(event)

            if len(self._buffer) >= self.cfg.flush_interval:
                self._flush_unlocked()

    def _flush_unlocked(self):
        """Buffer'ı diske yaz. Lock TUTULMALIDIR (deadlock önlemi)."""
        if not self._buffer:
            return

        if self.cfg.enable_csv_log:
            try:
                with open(self.csv_path, "a", newline="") as f:
                    writer = csv.writer(f)
                    for event in self._buffer:
                        cx, cy = event.get("center", (0, 0))
                        bbox = event.get("bbox", [0, 0, 0, 0])
                        try:
                            ts = datetime.fromtimestamp(event["timestamp"]).isoformat(
                                timespec="seconds"
                            )
                        except (KeyError, ValueError, OSError):
                            ts = datetime.now().isoformat(timespec="seconds")
                        writer.writerow([
                            ts,
                            event.get("track_id", ""),
                            cx, cy,
                            bbox[0], bbox[1], bbox[2], bbox[3],
                            event.get("confidence", 0),
                            event.get("total", 0),
                        ])
            except IOError as e:
                print(f"[LOGGER] CSV hatası: {e}")

        if self.cfg.enable_daily_total:
            try:
                with open(self.daily_path, "w") as f:
                    f.write(str(self._total_count) + "\n")
            except IOError as e:
                print(f"[LOGGER] Günlük toplam hatası: {e}")

        self._buffer.clear()

    def force_flush(self):
        with self._lock:
            self._flush_unlocked()

    def get_daily_total(self) -> int:
        return self._total_count

    def get_csv_path(self) -> str:
        return str(self.csv_path)

    def reset_counter(self):
        with self._lock:
            self._flush_unlocked()
            self._total_count = 0
            if self.cfg.enable_daily_total:
                try:
                    with open(self.daily_path, "w") as f:
                        f.write("0\n")
                except IOError:
                    pass
            print("[LOGGER] Sayaç sıfırlandı.")

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.force_flush()
        print(f"[LOGGER] Kapatıldı. Günlük toplam: {self._total_count}")

    def __del__(self):
        try:
            if not self._closed:
                self.close()
        except Exception:
            pass
