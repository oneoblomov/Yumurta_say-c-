"""
visualizer.py - Görsel İşaretleme (RPi5 Zero-Copy Optimize)
==============================================================
KRİTİK DÜZELTMELER:
  1. frame.copy() KALDIRILDI. Eski kod her frame'de N+3 tam kopya oluşturuyordu:
     - 1x overlay (tüm yumurtalar)
     - Nx label bg_overlay (HER YUMURTA İÇİN frame.copy()!)
     - 1x counting line margin
     - 1x HUD
     RPi5'te 30 yumurta = 33 × 640×480×3 = ~30MB/frame kopyalama!

  2. YENİ YAKLAŞIM: Sadece ROI bölgelerinde alpha blending.
     Tam frame kopyası yerine, her yumurtanın bbox ROI'sinde blend yapılır.
     Bu 100x-1000x daha az bellek kullanır.

  3. Label arkplan: Sadece küçük dikdörtgen ROI'de blend (frame.copy() yok).
  4. HUD: Sadece panel ROI'sinde blend.
"""

import cv2
import numpy as np
from typing import Dict, Optional, List

from .config import VisualizerConfig, CounterConfig


class Visualizer:
    """RPi5 optimize görselleştirme - zero frame copy."""

    def __init__(self, vis_cfg: VisualizerConfig, counter_cfg: CounterConfig):
        self.cfg = vis_cfg
        self.counter_cfg = counter_cfg
        self._font = cv2.FONT_HERSHEY_SIMPLEX

    def draw(self, frame: np.ndarray,
             detections: list,
             counting_line_y: int,
             roi_top_y: int = -1,
             roi_bottom_y: int = -1,
             total_count: int = 0,
             active_tracks: int = 0,
             fps: float = 0.0,
             frame_width: int = 0,
             trails: Optional[Dict[int, list]] = None,
             debug_mode: bool = False,
             show_trails: bool = True,
             newly_counted: Optional[list] = None) -> np.ndarray:
        """
        Tüm görsel öğeleri çiz. ZERO full-frame copy.
        """
        h, w = frame.shape[:2]

        # 1. Yarı saydam elips dolgu (ROI blend)
        for det in detections:
            self._draw_egg_overlay_roi(frame, det)

        # 2. Bounding box
        if self.cfg.show_bbox:
            for det in detections:
                self._draw_bbox(frame, det)

        # 3. Etiketler
        for det in detections:
            self._draw_label(frame, det)

        # 4. Trail çizgileri
        if show_trails and trails:
            for det in detections:
                tid = det.get("track_id")
                if tid and tid in trails:
                    self._draw_trail(frame, trails[tid], det.get("is_counted", False))

        # 5. Sayım çizgisi + ROI bandı
        self._draw_counting_line(frame, counting_line_y, w,
                                 roi_top_y=roi_top_y,
                                 roi_bottom_y=roi_bottom_y)

        # 6. Parlama efekti
        if newly_counted and self.cfg.enable_count_flash:
            for event in newly_counted:
                self._draw_count_flash(frame, event)

        # 7. HUD
        self._draw_hud(frame, total_count, active_tracks, fps, debug_mode)

        return frame

    def _draw_egg_overlay_roi(self, frame: np.ndarray, det: dict):
        """
        Yumurta üzerine yarı saydam elips dolgu - SADECE ROI'DE BLEND.

        ESKİ: overlay = frame.copy() + tüm frame addWeighted -> ~3ms/yumurta
        YENİ: Sadece bbox ROI'sinde blend -> ~0.1ms/yumurta
        """
        x1, y1, x2, y2 = det["bbox"]
        is_counted = det.get("is_counted", False)

        h, w = frame.shape[:2]
        # ROI sınırlarını kontrol et
        x1c, y1c = max(0, x1), max(0, y1)
        x2c, y2c = min(w, x2), min(h, y2)
        if x2c <= x1c or y2c <= y1c:
            return

        color = self.cfg.counted_color if is_counted else self.cfg.uncounted_color
        alpha = self.cfg.counted_alpha if is_counted else self.cfg.uncounted_alpha

        # ROI çıkar
        roi = frame[y1c:y2c, x1c:x2c]
        roi_overlay = roi.copy()  # Sadece küçük ROI kopyalanıyor, tam frame değil!

        # Elips çiz (ROI koordinatlarında)
        cx_roi = (x1 + x2) // 2 - x1c
        cy_roi = (y1 + y2) // 2 - y1c
        rx = (x2 - x1) // 2
        ry = (y2 - y1) // 2

        cv2.ellipse(roi_overlay, (cx_roi, cy_roi), (rx, ry), 0, 0, 360, color, -1)
        cv2.addWeighted(roi_overlay, alpha, roi, 1 - alpha, 0, roi)

    def _draw_bbox(self, frame: np.ndarray, det: dict):
        x1, y1, x2, y2 = det["bbox"]
        is_counted = det.get("is_counted", False)
        color = self.cfg.counted_color if is_counted else self.cfg.uncounted_color
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, self.cfg.bbox_thickness)

    def _draw_label(self, frame: np.ndarray, det: dict):
        """
        Etiket çiz. Düzeltme: frame.copy() YOK.
        Eski kod her etiket için tüm frame'i kopyalıyordu.
        """
        tid = det.get("track_id")
        if tid is None:
            return

        x1, y1 = det["bbox"][0], det["bbox"][1]
        is_counted = det.get("is_counted", False)

        if is_counted:
            label = f"#{tid} OK"
            color = self.cfg.counted_color
        else:
            conf = det.get("confidence", 0)
            label = f"#{tid} {conf:.0%}"
            color = self.cfg.uncounted_color

        (tw, th), _ = cv2.getTextSize(label, self._font, self.cfg.font_scale,
                                       self.cfg.font_thickness)

        label_y = max(y1 - 5, th + 5)
        label_x = x1

        if self.cfg.enable_label_bg:
            # ROI-only blend (eski: frame.copy())
            h, w = frame.shape[:2]
            bg_x1 = max(0, label_x)
            bg_y1 = max(0, label_y - th - 4)
            bg_x2 = min(w, label_x + tw + 4)
            bg_y2 = min(h, label_y + 4)
            if bg_x2 > bg_x1 and bg_y2 > bg_y1:
                roi = frame[bg_y1:bg_y2, bg_x1:bg_x2]
                bg = roi.copy()
                bg[:] = color
                cv2.addWeighted(bg, self.cfg.label_bg_alpha, roi,
                                1 - self.cfg.label_bg_alpha, 0, roi)

        cv2.putText(frame, label, (label_x + 2, label_y),
                    self._font, self.cfg.font_scale,
                    (255, 255, 255), self.cfg.font_thickness, cv2.LINE_AA)

    def _draw_trail(self, frame: np.ndarray, trail: list, is_counted: bool):
        if len(trail) < 2:
            return
        color = self.cfg.counted_color if is_counted else self.cfg.uncounted_color

        # Basitleştirilmiş trail: sadece polylines (eski: her segment ayrı renk hesabı)
        pts = np.array(trail, dtype=np.int32).reshape(-1, 1, 2)
        cv2.polylines(frame, [pts], False, color, 1, cv2.LINE_AA)

    def _draw_counting_line(self, frame: np.ndarray, line_y: int, frame_width: int,
                             roi_top_y: int = -1, roi_bottom_y: int = -1):
        h, w = frame.shape[:2]
        if frame_width <= 0:
            frame_width = w
        color = self.counter_cfg.line_color      # sarı (0,255,255)
        thickness = self.counter_cfg.line_thickness
        roi_color = self.counter_cfg.roi_line_color  # turuncu (0,165,255)
        roi_thickness = self.counter_cfg.roi_line_thickness

        # --- ROI BANDI (üst ve alt sınır arası saydam dolgu) ---
        if roi_top_y >= 0 and roi_bottom_y > roi_top_y:
            r1 = max(0, roi_top_y)
            r2 = min(h, roi_bottom_y)
            if r2 > r1:
                band = frame[r1:r2, :].copy()
                band[:] = roi_color
                cv2.addWeighted(band, 0.07, frame[r1:r2, :], 0.93, 0, frame[r1:r2, :])

            # Üst ROI çizgisi
            cv2.line(frame, (0, roi_top_y), (frame_width, roi_top_y),
                     roi_color, roi_thickness, cv2.LINE_AA)
            cv2.putText(frame, "ROI GIRIS", (10, roi_top_y - 6),
                        self._font, 0.38, roi_color, 1, cv2.LINE_AA)

            # Alt ROI çizgisi
            cv2.line(frame, (0, roi_bottom_y), (frame_width, roi_bottom_y),
                     roi_color, roi_thickness, cv2.LINE_AA)
            cv2.putText(frame, "ROI CIKIS", (10, roi_bottom_y + 14),
                        self._font, 0.38, roi_color, 1, cv2.LINE_AA)

        # --- Orta sayım çizgisi ---
        cv2.line(frame, (0, line_y), (frame_width, line_y), color, thickness, cv2.LINE_AA)

        # Margin zone - ROI blend (eski: frame.copy())
        margin = self.counter_cfg.crossing_margin
        if margin > 0:
            my1 = max(0, line_y - margin)
            my2 = min(h, line_y + margin)
            if my2 > my1:
                roi_slice = frame[my1:my2, :]
                zone = roi_slice.copy()
                zone[:] = color
                cv2.addWeighted(zone, 0.12, roi_slice, 0.88, 0, roi_slice)

        cv2.putText(frame, "", (10, line_y - 10),
                    self._font, 0.4, color, 1, cv2.LINE_AA)

    def _draw_count_flash(self, frame: np.ndarray, event: dict):
        cx, cy = event["center"]
        cv2.circle(frame, (cx, cy), 25, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, "+1", (cx + 15, cy - 15),
                    self._font, 0.7, (0, 255, 255), 2, cv2.LINE_AA)

    def _draw_hud(self, frame: np.ndarray, total_count: int,
                  active_tracks: int, fps: float, debug_mode: bool):
        """
        HUD paneli. Düzeltme: ROI blend (eski: frame.copy()).
        """
        if self.cfg.headless:
            return  # Web arayüzünde HUD gizle

        lines = [
            f"TOPLAM: {total_count}",
            f"AKTIF:  {active_tracks}",
            f"FPS:    {fps:.1f}",
        ]
        if debug_mode:
            h, w = frame.shape[:2]
            lines.append(f"RES:    {w}x{h}")

        line_h = 26
        pad = 8
        panel_w = 200
        panel_h = len(lines) * line_h + pad * 2

        # ROI blend
        fh, fw = frame.shape[:2]
        px2 = min(fw, 5 + panel_w)
        py2 = min(fh, 5 + panel_h)

        if px2 > 5 and py2 > 5:
            roi = frame[5:py2, 5:px2]
            bg = roi.copy()
            bg[:] = self.cfg.hud_bg_color
            cv2.addWeighted(bg, self.cfg.hud_bg_alpha, roi,
                            1 - self.cfg.hud_bg_alpha, 0, roi)

        cv2.rectangle(frame, (5, 5), (5 + panel_w, 5 + panel_h),
                      self.cfg.hud_color, 1)

        for i, line in enumerate(lines):
            y = 5 + pad + (i + 1) * line_h - 4
            if i == 0:
                cv2.putText(frame, line, (12, y), self._font,
                            self.cfg.hud_font_scale, (0, 255, 255), 2, cv2.LINE_AA)
            else:
                cv2.putText(frame, line, (12, y), self._font,
                            self.cfg.hud_font_scale, self.cfg.hud_color, 1, cv2.LINE_AA)

    def draw_debug_info(self, frame: np.ndarray, info: dict):
        h, w = frame.shape[:2]
        y_offset = h - 20
        for key, val in reversed(list(info.items())):
            text = f"{key}: {val}"
            cv2.putText(frame, text, (w - 280, y_offset),
                        self._font, 0.4, (200, 200, 200), 1, cv2.LINE_AA)
            y_offset -= 16
