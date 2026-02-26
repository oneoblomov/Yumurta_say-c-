"""
preprocessor.py - Hafif Ön İşleme Modülü (RPi5 Optimize)
==========================================================
Düzeltmeler:
  1. frame.copy() kaldırıldı (gereksizdi, in-place çalışıyor)
  2. ORB + BFMatcher artık her frame'de OLUŞTURULMUYOR (__init__'te bir kez)
  3. cvtColor çağrıları birleştirildi (3-4 yerine 1)
  4. brightness kontrolü her frame'de YAPILMIYOR (interval ile)
  5. Stabilizasyon varsayılan KAPALI (RPi5'te ~15ms/frame)
"""

import cv2
import numpy as np
from collections import deque
from typing import Optional

from .config import PreprocessorConfig


class FramePreprocessor:
    """RPi5 için hafif ön işleme."""

    def __init__(self, config: PreprocessorConfig):
        self.cfg = config
        self._frame_counter = 0

        # CLAHE nesnesi bir kez oluştur
        if self.cfg.enable_clahe:
            self._clahe = cv2.createCLAHE(
                clipLimit=self.cfg.clahe_clip_limit,
                tileGridSize=self.cfg.clahe_grid_size,
            )

        # Stabilizasyon nesneleri bir kez oluştur (ESKİ HATA: her frame'de yeniden oluşturuluyordu)
        if self.cfg.enable_stabilization:
            self._prev_gray: Optional[np.ndarray] = None
            self._orb = cv2.ORB_create(nfeatures=150)     # Bir kez oluştur
            self._bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)  # Bir kez oluştur
            self._transforms = deque(maxlen=self.cfg.stabilization_smoothing)

        # Adaptif parlaklık
        if self.cfg.adaptive_brightness:
            self._brightness_history = deque(maxlen=30)
            self._target_brightness: Optional[float] = None
            self._last_brightness_ratio = 1.0

    def process(self, frame: np.ndarray) -> np.ndarray:
        """
        Frame ön işleme. IN-PLACE değişiklik yapar, kopya oluşturmaz.

        ESKİ HATA: frame.copy() yapılıyordu -> gereksiz bellek kopyası.
        Şimdi doğrudan frame üzerinde çalışıyor.
        """
        self._frame_counter += 1

        # 1. Stabilizasyon (RPi5'te varsayılan kapalı)
        if self.cfg.enable_stabilization:
            frame = self._stabilize(frame)

        # 2. Adaptif parlaklık (her N frame'de bir kontrol)
        if self.cfg.adaptive_brightness:
            frame = self._normalize_brightness(frame)

        # 3. CLAHE
        if self.cfg.enable_clahe:
            frame = self._apply_clahe(frame)

        # 4. Denoise (RPi5'te varsayılan kapalı)
        if self.cfg.enable_denoise:
            frame = cv2.GaussianBlur(frame, (3, 3), 0)  # fastNlMeans yerine Gaussian (100x hızlı)

        return frame

    def _apply_clahe(self, frame: np.ndarray) -> np.ndarray:
        """LAB renk uzayında L kanalına CLAHE."""
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = self._clahe.apply(l)
        cv2.merge([l, a, b], lab)
        cv2.cvtColor(lab, cv2.COLOR_LAB2BGR, dst=frame)  # dst=frame -> kopya yok
        return frame

    def _normalize_brightness(self, frame: np.ndarray) -> np.ndarray:
        """
        Adaptif parlaklık normalizasyonu.
        Düzeltme: Her frame'de değil, her N frame'de kontrol.
        Son hesaplanan oranı cache'leyip diğer frame'lerde kullan.
        """
        interval = self.cfg.brightness_check_interval

        if self._frame_counter % interval == 0:
            # Tam frame mean yerine downscale edilmiş versiyonda hesapla (4x hızlı)
            small = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25, interpolation=cv2.INTER_NEAREST)
            gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            current_brightness = float(np.mean(gray_small))
            self._brightness_history.append(current_brightness)

            if self._target_brightness is None:
                self._target_brightness = current_brightness
                self._last_brightness_ratio = 1.0
                return frame

            avg_brightness = float(np.mean(self._brightness_history))
            self._target_brightness = 0.95 * self._target_brightness + 0.05 * avg_brightness

            ratio = self._target_brightness / max(current_brightness, 1.0)
            if abs(ratio - 1.0) > 0.15:
                self._last_brightness_ratio = 1.0 + (ratio - 1.0) * 0.4
            else:
                self._last_brightness_ratio = 1.0

        # Cache'lenmiş oranı uygula
        if abs(self._last_brightness_ratio - 1.0) > 0.02:
            frame = cv2.convertScaleAbs(frame, alpha=self._last_brightness_ratio, beta=0)

        return frame

    def _stabilize(self, frame: np.ndarray) -> np.ndarray:
        """
        ORB tabanlı stabilizasyon.
        Düzeltme: ORB ve BFMatcher __init__'te oluşturuluyor, her frame'de değil.
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if self._prev_gray is None:
            self._prev_gray = gray
            return frame

        try:
            kp1, des1 = self._orb.detectAndCompute(self._prev_gray, None)
            kp2, des2 = self._orb.detectAndCompute(gray, None)

            if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
                self._prev_gray = gray
                return frame

            matches = self._bf.match(des1, des2)
            if len(matches) < 8:
                self._prev_gray = gray
                return frame

            matches = sorted(matches, key=lambda x: x.distance)[:30]

            pts1 = np.float32([kp1[m.queryIdx].pt for m in matches])
            pts2 = np.float32([kp2[m.trainIdx].pt for m in matches])

            M, _ = cv2.estimateAffinePartial2D(pts1, pts2, method=cv2.RANSAC)

            if M is not None:
                dx = M[0, 2]
                dy = M[1, 2]
                da = np.arctan2(M[1, 0], M[0, 0])
                self._transforms.append((dx, dy, da))

                if len(self._transforms) >= 2:
                    avg_dx = np.mean([t[0] for t in self._transforms])
                    avg_dy = np.mean([t[1] for t in self._transforms])
                    avg_da = np.mean([t[2] for t in self._transforms])

                    if abs(dx - avg_dx) > 2.0 or abs(dy - avg_dy) > 2.0:
                        cos_a = np.cos(avg_da - da)
                        sin_a = np.sin(avg_da - da)
                        M_comp = np.float32([
                            [cos_a, -sin_a, avg_dx - dx],
                            [sin_a, cos_a, avg_dy - dy]
                        ])
                        h, w = frame.shape[:2]
                        frame = cv2.warpAffine(frame, M_comp, (w, h),
                                               borderMode=cv2.BORDER_REPLICATE)
                        # Düzeltme: Stabilize edilen frame'in gray'ini güncelle
                        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        except Exception:
            pass

        self._prev_gray = gray
        return frame

    def reset(self):
        self._frame_counter = 0
        if self.cfg.enable_stabilization:
            self._prev_gray = None
            self._transforms.clear()
        if self.cfg.adaptive_brightness:
            self._brightness_history.clear()
            self._target_brightness = None
            self._last_brightness_ratio = 1.0
