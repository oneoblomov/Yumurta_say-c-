"""
detector.py - YOLO Algılama Modülü
====================================
Eğitilmiş YOLO modeli ile yumurta algılama.
GPU hızlandırmalı, FP16 destekli, thread-safe.

NOT: Ultralytics YOLO, built-in ByteTrack desteği sunar.
     track() metodu hem algılama hem takip yapar.
"""

import numpy as np
from pathlib import Path
from typing import Optional
from ultralytics import YOLO

from .config import DetectorConfig, TrackerConfig


class EggDetector:
    """
    YOLO tabanlı yumurta algılama ve takip.

    Ultralytics'in entegre tracker desteğini kullanır (ByteTrack).
    Bu sayede algılama + takip tek inference çağrısında yapılır,
    ekstra overhead oluşmaz.

    Attributes:
        model: YOLO model nesnesi
        cfg: Algılama konfigürasyonu
        tracker_cfg: Takip konfigürasyonu
    """

    def __init__(self, det_cfg: DetectorConfig, tracker_cfg: TrackerConfig):
        self.cfg = det_cfg
        self.tracker_cfg = tracker_cfg

        # Model yükle
        model_path = Path(det_cfg.model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"Model dosyası bulunamadı: {model_path.absolute()}"
            )

        self.model = YOLO(str(model_path))

        # Model warmup (ilk frame gecikmesini önle)
        self._warmup()

    def _warmup(self):
        """GPU warmup - ilk frame gecikme problemini çözer."""
        import torch
        dummy = np.zeros((self.cfg.imgsz, self.cfg.imgsz, 3), dtype=np.uint8)
        try:
            self.model.predict(
                dummy,
                imgsz=self.cfg.imgsz,
                conf=self.cfg.conf_threshold,
                device=self.cfg.device,
                half=self.cfg.half,
                verbose=False,
            )
        except Exception:
            pass  # Warmup hatası kritik değil

    def detect_and_track(self, frame: np.ndarray, persist: bool = True):
        """
        Tek çağrıda algılama + ByteTrack takip.

        Args:
            frame: BGR formatında frame
            persist: İzleri frame'ler arası koru (True olmalı)

        Returns:
            Ultralytics Results nesnesi (boxes, track IDs dahil)
        """
        results = self.model.track(
            source=frame,
            imgsz=self.cfg.imgsz,
            conf=self.cfg.conf_threshold,
            iou=self.cfg.iou_threshold,
            max_det=self.cfg.max_det,
            device=self.cfg.device,
            half=self.cfg.half,
            agnostic_nms=self.cfg.agnostic_nms,
            classes=self.cfg.classes,
            persist=persist,
            tracker=self.tracker_cfg.tracker_type + ".yaml",
            verbose=False,
        )
        return results[0] if results else None

    def detect_only(self, frame: np.ndarray):
        """
        Sadece algılama (takipsiz). Debug / test için.

        Args:
            frame: BGR formatında frame

        Returns:
            Ultralytics Results nesnesi
        """
        results = self.model.predict(
            source=frame,
            imgsz=self.cfg.imgsz,
            conf=self.cfg.conf_threshold,
            iou=self.cfg.iou_threshold,
            max_det=self.cfg.max_det,
            device=self.cfg.device,
            half=self.cfg.half,
            verbose=False,
        )
        return results[0] if results else None

    @staticmethod
    def parse_results(result) -> list:
        """
        YOLO sonuçlarını standart formata çevir.

        Returns:
            List of dict: [
                {
                    "track_id": int | None,
                    "bbox": [x1, y1, x2, y2],
                    "center": (cx, cy),
                    "confidence": float,
                    "class_id": int,
                    "class_name": str,
                }
            ]
        """
        detections = []
        if result is None or result.boxes is None or len(result.boxes) == 0:
            return detections

        boxes = result.boxes
        for i in range(len(boxes)):
            x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy().astype(int)
            conf = float(boxes.conf[i].cpu().numpy())
            cls_id = int(boxes.cls[i].cpu().numpy())

            # Track ID (varsa)
            track_id = None
            if boxes.id is not None:
                track_id = int(boxes.id[i].cpu().numpy())

            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2

            detections.append({
                "track_id": track_id,
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                "center": (int(cx), int(cy)),
                "confidence": round(conf, 3),
                "class_id": cls_id,
                "class_name": result.names[cls_id] if result.names else "egg",
            })

        return detections
