"""
visualizer.py - Görsel İşaretleme Modülü
==========================================
Yarı saydam maskeleme, HUD overlay, trail çizimi.

Renk kodlaması:
  🔴 Kırmızı yarı saydam dolgu -> Henüz sayılmamış yumurta
  🟢 Yeşil yarı saydam dolgu   -> Sayılmış yumurta

HUD gösterir:
  - Toplam sayım
  - Aktif takip sayısı
  - FPS
  - Debug bilgisi
"""

import cv2
import numpy as np
from typing import List, Dict, Tuple, Optional

from .config import VisualizerConfig, CounterConfig


class Visualizer:
    """
    Production-ready görsel işaretleme sistemi.

    Performans için overlay sadece gerekli bölgelere uygulanır.
    Alpha blending ile yarı saydam maskeleme.
    """

    def __init__(self, vis_cfg: VisualizerConfig, counter_cfg: CounterConfig):
        self.cfg = vis_cfg
        self.counter_cfg = counter_cfg
        self._font = cv2.FONT_HERSHEY_SIMPLEX

    def draw(self, frame: np.ndarray,
             detections: list,
             counting_line_y: int,
             total_count: int,
             active_tracks: int,
             fps: float,
             frame_width: int,
             trails: Optional[Dict[int, list]] = None,
             debug_mode: bool = False,
             show_trails: bool = True,
             newly_counted: Optional[list] = None) -> np.ndarray:
        """
        Tüm görsel öğeleri frame üzerine çiz.

        Args:
            frame: BGR frame (in-place değiştirilir)
            detections: Zenginleştirilmiş algılama listesi
            counting_line_y: Sayım çizgisi Y pozisyonu
            total_count: Toplam sayım
            active_tracks: Aktif takip sayısı
            fps: Anlık FPS
            frame_width: Frame genişliği
            trails: ID -> [(cx,cy)] trail verileri
            debug_mode: Debug bilgisi göster
            show_trails: İz çizgilerini göster
            newly_counted: Bu frame'de sayılan yumurtalar

        Returns:
            İşaretlenmiş frame
        """
        overlay = frame.copy()

        # 1. Yarı saydam maskeleme (her yumurta için)
        for det in detections:
            self._draw_egg_overlay(overlay, det)

        # Overlay'ı ana frame ile birleştir
        # Tüm yarı saydam çizimleri tek seferde blend et
        alpha = max(self.cfg.uncounted_alpha, self.cfg.counted_alpha)
        cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

        # 2. Etiketler (yarı saydam değil, net olmalı)
        for det in detections:
            self._draw_label(frame, det)

        # 3. Bounding box (opsiyonel)
        if self.cfg.show_bbox:
            for det in detections:
                self._draw_bbox(frame, det)

        # 4. Trail çizgileri
        if show_trails and trails:
            for det in detections:
                tid = det.get("track_id")
                if tid and tid in trails:
                    self._draw_trail(frame, trails[tid], det.get("is_counted", False))

        # 5. Sayım çizgisi
        self._draw_counting_line(frame, counting_line_y, frame_width)

        # 6. Yeni sayılan yumurtalarda parlama efekti
        if newly_counted:
            for event in newly_counted:
                self._draw_count_flash(frame, event)

        # 7. HUD panel
        self._draw_hud(frame, total_count, active_tracks, fps, debug_mode)

        return frame

    def _draw_egg_overlay(self, overlay: np.ndarray, det: dict):
        """Yumurta üzerine yarı saydam dolgu çiz."""
        x1, y1, x2, y2 = det["bbox"]
        is_counted = det.get("is_counted", False)

        color = self.cfg.counted_color if is_counted else self.cfg.uncounted_color

        # Eliptik dolgu (yumurta şekline daha uygun)
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        rx = (x2 - x1) // 2
        ry = (y2 - y1) // 2

        cv2.ellipse(overlay, (cx, cy), (rx, ry), 0, 0, 360, color, -1)

    def _draw_bbox(self, frame: np.ndarray, det: dict):
        """Bounding box çiz."""
        x1, y1, x2, y2 = det["bbox"]
        is_counted = det.get("is_counted", False)

        color = self.cfg.counted_color if is_counted else self.cfg.uncounted_color
        thickness = self.cfg.bbox_thickness

        cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)

    def _draw_label(self, frame: np.ndarray, det: dict):
        """ID veya COUNTED etiketi çiz."""
        tid = det.get("track_id")
        if tid is None:
            return

        x1, y1, x2, y2 = det["bbox"]
        is_counted = det.get("is_counted", False)

        # Etiket metni
        if is_counted:
            label = f"#{tid} COUNTED"
            color = self.cfg.counted_color
        else:
            conf = det.get("confidence", 0)
            label = f"#{tid} {conf:.0%}"
            color = self.cfg.uncounted_color

        # Metin boyutu hesapla
        (tw, th), baseline = cv2.getTextSize(
            label, self._font, self.cfg.font_scale, self.cfg.font_thickness
        )

        # Etiket arkaplanı (yarı saydam)
        label_y = max(y1 - 5, th + 5)
        label_x = x1

        # Arkaplan dikdörtgeni
        bg_overlay = frame.copy()
        cv2.rectangle(
            bg_overlay,
            (label_x, label_y - th - 4),
            (label_x + tw + 4, label_y + 4),
            color, -1
        )
        cv2.addWeighted(bg_overlay, self.cfg.label_bg_alpha,
                        frame, 1 - self.cfg.label_bg_alpha, 0, frame)

        # Metin (beyaz)
        cv2.putText(
            frame, label,
            (label_x + 2, label_y),
            self._font, self.cfg.font_scale,
            (255, 255, 255), self.cfg.font_thickness,
            cv2.LINE_AA
        )

    def _draw_trail(self, frame: np.ndarray, trail: list, is_counted: bool):
        """Yumurtanın hareket izini çiz."""
        if len(trail) < 2:
            return

        color = self.cfg.counted_color if is_counted else self.cfg.uncounted_color

        for i in range(1, len(trail)):
            # İz kalınlığı zamanla artar (yeni noktalar kalın)
            thickness = max(1, int(i / len(trail) * 3))
            # İz transparanlığı için renk soluklaştır
            factor = i / len(trail)
            faded_color = tuple(int(c * factor) for c in color)

            pt1 = (int(trail[i - 1][0]), int(trail[i - 1][1]))
            pt2 = (int(trail[i][0]), int(trail[i][1]))
            cv2.line(frame, pt1, pt2, faded_color, thickness, cv2.LINE_AA)

    def _draw_counting_line(self, frame: np.ndarray, line_y: int, frame_width: int):
        """Sayım çizgisini çiz."""
        color = self.counter_cfg.line_color
        thickness = self.counter_cfg.line_thickness

        # Ana çizgi
        cv2.line(frame, (0, line_y), (frame_width, line_y), color, thickness, cv2.LINE_AA)

        # Margin bölgesi (yarı saydam)
        margin = self.counter_cfg.crossing_margin
        if margin > 0:
            overlay = frame.copy()
            cv2.rectangle(overlay, (0, line_y - margin),
                          (frame_width, line_y + margin), color, -1)
            cv2.addWeighted(overlay, 0.15, frame, 0.85, 0, frame)

        # Çizgi etiketi
        line_label = f"COUNTING LINE (y={line_y})"
        cv2.putText(frame, line_label, (10, line_y - 10),
                    self._font, 0.4, color, 1, cv2.LINE_AA)

    def _draw_count_flash(self, frame: np.ndarray, event: dict):
        """Yeni sayılan yumurtada parlama efekti."""
        cx, cy = event["center"]
        # Beyaz halka efekti
        cv2.circle(frame, (cx, cy), 30, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.circle(frame, (cx, cy), 35, (0, 255, 255), 2, cv2.LINE_AA)

        # "+1" göstergesi
        cv2.putText(frame, "+1", (cx + 20, cy - 20),
                    self._font, 0.8, (0, 255, 255), 2, cv2.LINE_AA)

    def _draw_hud(self, frame: np.ndarray, total_count: int,
                  active_tracks: int, fps: float, debug_mode: bool):
        """
        Head-Up Display paneli çiz.

        Sol üst: Toplam sayım, aktif takip, FPS
        """
        h, w = frame.shape[:2]
        hud_lines = [
            f"TOPLAM: {total_count}",
            f"AKTIF:  {active_tracks}",
            f"FPS:    {fps:.1f}",
        ]

        if debug_mode:
            hud_lines.append(f"RES:    {w}x{h}")

        # Panel boyutu hesapla
        line_height = 30
        padding = 10
        panel_w = 220
        panel_h = len(hud_lines) * line_height + padding * 2

        # Yarı saydam arkaplan
        overlay = frame.copy()
        cv2.rectangle(overlay, (5, 5), (5 + panel_w, 5 + panel_h),
                      self.cfg.hud_bg_color, -1)
        cv2.addWeighted(overlay, self.cfg.hud_bg_alpha,
                        frame, 1 - self.cfg.hud_bg_alpha, 0, frame)

        # Panel çerçevesi
        cv2.rectangle(frame, (5, 5), (5 + panel_w, 5 + panel_h),
                      self.cfg.hud_color, 1)

        # Metin satırları
        for i, line in enumerate(hud_lines):
            y = 5 + padding + (i + 1) * line_height - 5

            # TOPLAM satırı için büyük font
            if i == 0:
                cv2.putText(frame, line, (15, y),
                            self._font, self.cfg.hud_font_scale + 0.1,
                            (0, 255, 255), 2, cv2.LINE_AA)
            else:
                cv2.putText(frame, line, (15, y),
                            self._font, self.cfg.hud_font_scale,
                            self.cfg.hud_color, 1, cv2.LINE_AA)

    def draw_debug_info(self, frame: np.ndarray, info: dict):
        """
        Debug modu ek bilgileri.
        Sağ alt köşede gösterilir.
        """
        h, w = frame.shape[:2]
        y_offset = h - 20

        for key, val in reversed(list(info.items())):
            text = f"{key}: {val}"
            cv2.putText(frame, text, (w - 300, y_offset),
                        self._font, 0.4, (200, 200, 200), 1, cv2.LINE_AA)
            y_offset -= 18
