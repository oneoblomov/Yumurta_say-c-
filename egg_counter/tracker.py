"""
tracker.py - Takip Yardımcı Modülü
====================================
ByteTrack/BotSORT sonuçları üzerine ek katmanlar:
  - İz (trail) geçmişi yönetimi
  - Track yaş takibi (min_track_age filtresi)
  - Kayıp iz temizliği

NOT: Asıl takip Ultralytics YOLO.track() içinde yapılır.
     Bu modül takip sonuçlarını zenginleştirir.
"""

from collections import defaultdict, deque
from typing import Dict, Tuple, Optional, Set
import time

from .config import TrackerConfig, CounterConfig


class TrackManager:
    """
    Takip ID'lerinin yaşam döngüsünü yönetir.

    Her ID için:
      - Konum geçmişi (trail)
      - İlk görülme zamanı
      - Son görülme frame'i
      - Sayılma durumu

    Attributes:
        trails: Her ID'nin merkez noktası geçmişi
        first_seen: Her ID'nin ilk görüldüğü frame
        last_seen: Her ID'nin son görüldüğü frame
        counted_ids: Sayılmış ID seti
        track_ages: Her ID'nin kaç frame'dir takip edildiği
    """

    def __init__(self, tracker_cfg: TrackerConfig, counter_cfg: CounterConfig,
                 trail_length: int = 30):
        self.tracker_cfg = tracker_cfg
        self.counter_cfg = counter_cfg
        self.trail_length = trail_length

        # ID -> deque of (cx, cy)
        self.trails: Dict[int, deque] = defaultdict(
            lambda: deque(maxlen=self.trail_length)
        )

        # ID -> frame numarası
        self.first_seen: Dict[int, int] = {}
        self.last_seen: Dict[int, int] = {}

        # Sayılmış ID'ler
        self.counted_ids: Set[int] = set()

        # ID -> sayılma frame'i (çift sayım koruması)
        self.counted_at_frame: Dict[int, int] = {}

        # Track yaş sayacı
        self.track_ages: Dict[int, int] = defaultdict(int)

        # Frame sayacı
        self._frame_count: int = 0

        # Aktif ID'ler (bu frame'de görülen)
        self._active_ids: Set[int] = set()

        # Önceki frame'deki y pozisyonları (yön tespiti için)
        self._prev_cy: Dict[int, int] = {}

    def update(self, detections: list) -> list:
        """
        Yeni frame algılamalarını işle.

        Args:
            detections: detector.parse_results() çıktısı

        Returns:
            Zenginleştirilmiş algılama listesi (ek alanlar: track_age, is_counted, direction)
        """
        self._frame_count += 1
        self._active_ids.clear()

        enriched = []
        for det in detections:
            tid = det.get("track_id")
            if tid is None:
                enriched.append({**det, "track_age": 0, "is_counted": False, "direction": None})
                continue

            cx, cy = det["center"]
            self._active_ids.add(tid)

            # Trail güncelle
            self.trails[tid].append((cx, cy))

            # İlk/son görülme güncelle
            if tid not in self.first_seen:
                self.first_seen[tid] = self._frame_count

            self.last_seen[tid] = self._frame_count
            self.track_ages[tid] += 1

            # Hareket yönü
            prev_cy = self._prev_cy.get(tid)
            direction = None
            if prev_cy is not None:
                if cy > prev_cy:
                    direction = "down"
                elif cy < prev_cy:
                    direction = "up"
                else:
                    direction = "stationary"

            self._prev_cy[tid] = cy

            enriched.append({
                **det,
                "track_age": self.track_ages[tid],
                "is_counted": tid in self.counted_ids,
                "direction": direction,
            })

        # Eski izleri temizle (track_buffer frame boyunca görülmeyenler)
        self._cleanup_stale_tracks()

        return enriched

    def mark_counted(self, track_id: int):
        """ID'yi sayılmış olarak işaretle."""
        self.counted_ids.add(track_id)
        self.counted_at_frame[track_id] = self._frame_count

    def can_be_counted(self, track_id: int) -> bool:
        """
        Bu ID sayılabilir mi?
        - Daha önce sayılmamış olmalı
        - Minimum takip yaşını geçmiş olmalı
        - Çift sayım cooldown'ı geçmiş olmalı
        """
        if track_id in self.counted_ids:
            return False

        if self.track_ages.get(track_id, 0) < self.counter_cfg.min_track_age:
            return False

        # Çift sayım koruması
        if track_id in self.counted_at_frame:
            frames_since = self._frame_count - self.counted_at_frame[track_id]
            if frames_since < self.counter_cfg.double_count_cooldown:
                return False

        return True

    def get_trail(self, track_id: int) -> list:
        """ID'nin konum geçmişini döndür."""
        return list(self.trails.get(track_id, []))

    def get_active_count(self) -> int:
        """Aktif takip edilen yumurta sayısı."""
        return len(self._active_ids)

    def get_total_counted(self) -> int:
        """Toplam sayılan yumurta."""
        return len(self.counted_ids)

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def _cleanup_stale_tracks(self):
        """Uzun süredir görülmeyen izleri temizle (bellek yönetimi)."""
        stale_threshold = self.tracker_cfg.track_buffer * 2
        stale_ids = [
            tid for tid, last in self.last_seen.items()
            if (self._frame_count - last) > stale_threshold
            and tid not in self._active_ids
        ]
        for tid in stale_ids:
            # Sayılmış ID'leri silme, sadece trail/age verilerini temizle
            self.trails.pop(tid, None)
            self.track_ages.pop(tid, None)
            self._prev_cy.pop(tid, None)
            # first_seen ve last_seen'ı tut (log için gerekebilir)

    def reset(self):
        """Tüm takip verilerini sıfırla."""
        self.trails.clear()
        self.first_seen.clear()
        self.last_seen.clear()
        self.counted_ids.clear()
        self.counted_at_frame.clear()
        self.track_ages.clear()
        self._frame_count = 0
        self._active_ids.clear()
        self._prev_cy.clear()
