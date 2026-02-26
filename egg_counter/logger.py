"""
logger.py - Log Sistemi Modülü
================================
Endüstriyel üretim ortamı için güvenilir log tutma.

Çıktılar:
  1. CSV event log: Her sayım olayının detaylı kaydı
  2. Günlük toplam: Gün sonu toplam sayım
  3. Konsol log: Gerçek zamanlı durum

Thread-safe file I/O ile veri kaybı önlenir.
"""

import csv
import os
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List

from .config import LoggerConfig


class CountLogger:
    """
    Production-ready sayım log sistemi.

    CSV log formatı:
        timestamp, track_id, cx, cy, x1, y1, x2, y2, confidence, total

    Günlük toplam formatı:
        Düz metin dosyada toplam sayı

    Attributes:
        log_dir: Log dizini
        csv_path: Güncel CSV dosyası yolu
        daily_path: Güncel günlük toplam dosyası yolu
    """

    def __init__(self, config: LoggerConfig):
        self.cfg = config
        self._lock = threading.Lock()
        self._buffer: List[dict] = []
        self._total_count: int = 0
        self._current_date: date = date.today()

        # Log dizini oluştur
        self.log_dir = Path(config.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Dosya yolları
        self._update_file_paths()

        # CSV başlığını yaz (yeni dosya ise)
        if self.cfg.enable_csv_log:
            self._ensure_csv_header()

    def _update_file_paths(self):
        """Tarih bazlı dosya yollarını güncelle."""
        date_str = self._current_date.isoformat()
        self.csv_path = self.log_dir / f"{self.cfg.csv_prefix}_{date_str}.csv"
        self.daily_path = self.log_dir / f"{self.cfg.daily_prefix}_{date_str}.txt"

    def _ensure_csv_header(self):
        """CSV dosyasına başlık satırını yaz (yoksa)."""
        if not self.csv_path.exists():
            with open(self.csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "track_id", "cx", "cy",
                    "x1", "y1", "x2", "y2", "confidence", "total"
                ])

    def log_count_event(self, event: dict):
        """
        Sayım olayını logla.

        Args:
            event: {
                "track_id": int,
                "center": (cx, cy),
                "bbox": [x1, y1, x2, y2],
                "confidence": float,
                "total": int,
                "timestamp": float,
            }
        """
        self._total_count = event.get("total", self._total_count)

        # Gün değişimi kontrolü
        today = date.today()
        if today != self._current_date:
            self._handle_day_change(today)

        with self._lock:
            self._buffer.append(event)

            # Flush interval'a ulaşıldığında diske yaz
            if len(self._buffer) >= self.cfg.flush_interval:
                self._flush()

    def _flush(self):
        """Buffer'daki logları diske yaz."""
        if not self._buffer:
            return

        # CSV log
        if self.cfg.enable_csv_log:
            try:
                with open(self.csv_path, "a", newline="") as f:
                    writer = csv.writer(f)
                    for event in self._buffer:
                        cx, cy = event.get("center", (0, 0))
                        bbox = event.get("bbox", [0, 0, 0, 0])
                        ts = datetime.fromtimestamp(event["timestamp"]).isoformat(
                            timespec="seconds"
                        )
                        writer.writerow([
                            ts,
                            event.get("track_id", ""),
                            cx, cy,
                            bbox[0], bbox[1], bbox[2], bbox[3],
                            event.get("confidence", 0),
                            event.get("total", 0),
                        ])
            except IOError as e:
                print(f"[LOGGER] CSV yazma hatası: {e}")

        # Günlük toplam
        if self.cfg.enable_daily_total:
            try:
                with open(self.daily_path, "w") as f:
                    f.write(str(self._total_count) + "\n")
            except IOError as e:
                print(f"[LOGGER] Günlük toplam yazma hatası: {e}")

        # Buffer temizle
        self._buffer.clear()

        # Konsol log
        print(f"[LOGGER] Flush: {self._total_count} toplam sayım kaydedildi.")

    def _handle_day_change(self, new_date: date):
        """
        Gün değişiminde:
        1. Mevcut buffer'ı flush et
        2. Yeni dosya yollarını ayarla
        3. Sayacı sıfırla
        """
        with self._lock:
            self._flush()
            self._current_date = new_date
            self._update_file_paths()
            self._total_count = 0
            if self.cfg.enable_csv_log:
                self._ensure_csv_header()
            print(f"[LOGGER] Yeni gün: {new_date.isoformat()}")

    def force_flush(self):
        """Buffer'ı zorla diske yaz."""
        with self._lock:
            self._flush()

    def get_daily_total(self) -> int:
        """Günün toplam sayımını döndür."""
        return self._total_count

    def get_csv_path(self) -> str:
        """Aktif CSV dosya yolunu döndür."""
        return str(self.csv_path)

    def reset_counter(self):
        """
        Sayacı sıfırla.
        Mevcut logları kaybetmez, yeni sayımlar 0'dan başlar.
        """
        with self._lock:
            self._flush()
            self._total_count = 0
            if self.cfg.enable_daily_total:
                try:
                    with open(self.daily_path, "w") as f:
                        f.write("0\n")
                except IOError:
                    pass
            print("[LOGGER] Sayaç sıfırlandı.")

    def close(self):
        """Logger'ı kapat, kalan buffer'ı flush et."""
        self.force_flush()
        print(f"[LOGGER] Kapatıldı. Günlük toplam: {self._total_count}")

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
