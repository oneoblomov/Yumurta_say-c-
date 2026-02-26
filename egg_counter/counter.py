"""
counter.py - Sanal Çizgi Sayım Modülü
=======================================
Ekranın ortasından geçen yatay sanal çizgi ile yumurta sayımı.

Sayım mantığı:
  1. Her yumurtanın bounding box merkezi (cx, cy) hesaplanır.
  2. Önceki frame'deki cy ile mevcut cy karşılaştırılır.
  3. Çizgiyi belirlenen yönde geçen yumurta 1 kez sayılır.
  4. Aynı ID tekrar geçse bile sayılmaz (çift sayım koruması).

Çizgi kesişim kontrolü:
  prev_cy < line_y <= curr_cy  (yukarıdan aşağıya)
  prev_cy > line_y >= curr_cy  (aşağıdan yukarıya)
"""

from typing import List, Dict, Tuple, Optional, Callable
import time

from .config import CounterConfig
from .tracker import TrackManager


class CountingLine:
    """
    Sanal sayım çizgisi.

    Çizgi, frame yüksekliğinin belirli bir oranında yatay olarak konumlandırılır.
    Yumurtaların bbox merkezi bu çizgiyi geçtiğinde sayım tetiklenir.

    Attributes:
        line_y: Çizginin piksel Y pozisyonu
        total_count: Toplam sayım
        _on_count_callbacks: Sayım gerçekleştiğinde çağrılacak callback'ler
    """

    def __init__(self, config: CounterConfig, frame_height: int):
        self.cfg = config
        self.frame_height = frame_height

        # Çizgi Y pozisyonu
        self.line_y = int(frame_height * config.line_position)

        # Sayaç
        self.total_count: int = 0

        # Sayım event callback'leri
        self._on_count_callbacks: List[Callable] = []

    def get_line_coords(self, frame_width: int) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        """Çizginin başlangıç ve bitiş noktalarını döndür."""
        return (0, self.line_y), (frame_width, self.line_y)

    def check_crossings(self, enriched_detections: list,
                        track_manager: TrackManager) -> List[Dict]:
        """
        Tüm algılamaları kontrol et ve çizgi geçişlerini tespit et.

        Args:
            enriched_detections: tracker.update() çıktısı (zenginleştirilmiş)
            track_manager: TrackManager referansı

        Returns:
            Bu frame'de sayılan yumurtaların detay listesi
        """
        newly_counted = []

        for det in enriched_detections:
            tid = det.get("track_id")
            if tid is None:
                continue

            # Zaten sayıldı mı?
            if det.get("is_counted", False):
                continue

            # Sayılabilir mi?
            if not track_manager.can_be_counted(tid):
                continue

            # Trail'den önceki pozisyonu al
            trail = track_manager.get_trail(tid)
            if len(trail) < 2:
                continue

            # Mevcut ve önceki y pozisyonları
            _, curr_cy = trail[-1]
            _, prev_cy = trail[-2]

            # Çizgi geçiş kontrolü (margin ile)
            crossed = False
            margin = self.cfg.crossing_margin
            line_y = self.line_y

            if self.cfg.direction in ("top_to_bottom", "both"):
                # Yukarıdan aşağıya geçiş
                if prev_cy < (line_y - margin) and curr_cy >= (line_y - margin):
                    crossed = True

            if self.cfg.direction in ("bottom_to_top", "both"):
                # Aşağıdan yukarıya geçiş
                if prev_cy > (line_y + margin) and curr_cy <= (line_y + margin):
                    crossed = True

            if crossed:
                # SAYIM!
                self.total_count += 1
                track_manager.mark_counted(tid)

                count_event = {
                    "track_id": tid,
                    "center": det["center"],
                    "bbox": det["bbox"],
                    "confidence": det["confidence"],
                    "total": self.total_count,
                    "timestamp": time.time(),
                    "direction": det.get("direction", "unknown"),
                }
                newly_counted.append(count_event)

                # Callback'leri çağır
                for cb in self._on_count_callbacks:
                    try:
                        cb(count_event)
                    except Exception:
                        pass

        return newly_counted

    def on_count(self, callback: Callable):
        """
        Sayım event callback ekle.
        Her sayımda callback(count_event) çağrılır.
        """
        self._on_count_callbacks.append(callback)

    def reset(self):
        """Sayacı sıfırla."""
        self.total_count = 0

    def update_line_position(self, new_position: float):
        """Çizgi pozisyonunu güncelle (0.0 - 1.0)."""
        self.cfg.line_position = max(0.05, min(0.95, new_position))
        self.line_y = int(self.frame_height * self.cfg.line_position)

    def update_frame_height(self, new_height: int):
        """Frame yüksekliği değiştiğinde çizgiyi güncelle."""
        self.frame_height = new_height
        self.line_y = int(new_height * self.cfg.line_position)
