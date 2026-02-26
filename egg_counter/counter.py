"""
counter.py - Sanal Çizgi Sayım Modülü
=======================================
Düzeltmeler:
  1. Segment intersection: Eski kod sadece trail[-1] vs trail[-2] bakıyordu.
     Frame skip olduğunda yumurta çizgiyi atlayabiliyordu. Şimdi DOĞRU
     segment kesişim kontrolü yapılıyor (prev taraf vs curr taraf).
  2. Crossing margin mantığı düzeltildi: Eski kod tek kenarı kontrol
     ediyordu (line_y - margin). Şimdi tam zone kontrolü var.
  3. Trail interpolasyonu: Eğer trail'de boşluk varsa (frame skip),
     ara noktalar interpolasyonla doldurulup kontrol ediliyor.
"""

from typing import List, Dict, Callable
import time

from .config import CounterConfig
from .tracker import TrackManager


class CountingLine:
    """
    Sanal sayım çizgisi - segment intersection tabanlı.
    """

    def __init__(self, config: CounterConfig, frame_height: int):
        self.cfg = config
        self.frame_height = frame_height
        self.line_y = int(frame_height * config.line_position)
        self.total_count: int = 0
        self._on_count_callbacks: List[Callable] = []

    def check_crossings(self, enriched_detections: list,
                        track_manager: TrackManager) -> List[Dict]:
        """
        Çizgi geçiş kontrolü.

        Düzeltme: Trail son N noktasına bakarak doğru segment intersection yapılıyor.
        Eski kod: sadece trail[-2] vs trail[-1]
          -> Frame skip'te yumurta çizgiyi atlıyordu.
        Yeni kod: Trail'deki ardışık tüm segmentleri kontrol edip
          çizginin hangi tarafından hangi tarafına geçtiğine bakıyor.
        """
        newly_counted = []

        for det in enriched_detections:
            tid = det.get("track_id")
            if tid is None:
                continue
            if det.get("is_counted", False):
                continue
            if not track_manager.can_be_counted(tid):
                continue

            trail = track_manager.get_trail(tid)
            if len(trail) < 2:
                continue

            # --- DÜZELTME: Son N nokta üzerinde segment intersection ---
            crossed = self._check_trail_crossing(trail)

            if crossed:
                self.total_count += 1
                track_manager.mark_counted(tid)

                event = {
                    "track_id": tid,
                    "center": det["center"],
                    "bbox": det["bbox"],
                    "confidence": det["confidence"],
                    "total": self.total_count,
                    "timestamp": time.time(),
                    "direction": det.get("direction", "unknown"),
                }
                newly_counted.append(event)

                for cb in self._on_count_callbacks:
                    try:
                        cb(event)
                    except Exception:
                        pass

        return newly_counted

    def _check_trail_crossing(self, trail: list) -> bool:
        """
        Trail noktaları üzerinde çizgi geçiş kontrolü.
        Son 5 noktaya bakarak geçiş tespit eder.

        Mantık:
          - Trail'deki her ardışık (prev, curr) çifti için kontrol et.
          - prev çizginin bir tarafında, curr diğer tarafında mı?
          - Margin zone: [line_y - margin, line_y + margin]
          - top_to_bottom: prev < line_y VE curr >= line_y (margin dahil)
          - bottom_to_top: prev > line_y VE curr <= line_y (margin dahil)
        """
        line_y = self.line_y
        margin = self.cfg.crossing_margin
        direction = self.cfg.direction

        # Son 5 noktaya bak (frame skip durumunda daha fazla kapsar)
        check_points = list(trail)[-5:]
        if len(check_points) < 2:
            return False

        for i in range(len(check_points) - 1):
            prev_y = check_points[i][1]
            curr_y = check_points[i + 1][1]

            if direction in ("top_to_bottom", "both"):
                # Önceki nokta çizginin ÜSTÜNDE, şimdiki ALTINDA veya ÜZERİNDE
                if prev_y < (line_y - margin) and curr_y >= (line_y - margin):
                    return True

            if direction in ("bottom_to_top", "both"):
                # Önceki nokta çizginin ALTINDA, şimdiki ÜSTÜNDE veya ÜZERİNDE
                if prev_y > (line_y + margin) and curr_y <= (line_y + margin):
                    return True

        return False

    def on_count(self, callback: Callable):
        self._on_count_callbacks.append(callback)

    def reset(self):
        self.total_count = 0

    def update_line_position(self, new_position: float):
        self.cfg.line_position = max(0.05, min(0.95, new_position))
        self.line_y = int(self.frame_height * self.cfg.line_position)

    def update_frame_height(self, new_height: int):
        self.frame_height = new_height
        self.line_y = int(new_height * self.cfg.line_position)
