"""
detector.py - YOLO Algılama + Takip Modülü
============================================
Kritik düzeltmeler:
  1. Özel ByteTrack YAML oluşturuluyor (TrackerConfig parametreleri gerçekten uygulanıyor)
  2. parse_results() batch tensor dönüşümü (RPi5 hız)
  3. Warmup track() ile yapılıyor (predict ile değil)
  4. NCNN/ONNX model desteği eklendi
"""

import os
import tempfile
import numpy as np
from pathlib import Path
from typing import Optional

from .config import DetectorConfig, TrackerConfig


def _create_custom_tracker_yaml(cfg: TrackerConfig) -> str:
    """
    TrackerConfig'den özel ByteTrack/BotSORT YAML dosyası oluştur.

    ESKİ HATA: Varsayılan bytetrack.yaml kullanılıyordu, tüm özel
    parametreler (track_buffer, match_thresh vb.) yok sayılıyordu.
    Bu fonksiyon parametreleri gerçek YAML'a yazar.

    Returns:
        Oluşturulan YAML dosya yolu
    """
    if cfg.tracker_type == "bytetrack":
        yaml_content = f"""# Özel ByteTrack konfigürasyonu - Yumurta Sayıcı
tracker_type: bytetrack
track_high_thresh: {cfg.track_high_thresh}
track_low_thresh: {cfg.track_low_thresh}
new_track_thresh: {cfg.new_track_thresh}
track_buffer: {cfg.track_buffer}
match_thresh: {cfg.match_thresh}
fuse_score: {str(cfg.fuse_score).lower()}
"""
    else:  # botsort
        yaml_content = f"""# Özel BotSORT konfigürasyonu - Yumurta Sayıcı
tracker_type: botsort
track_high_thresh: {cfg.track_high_thresh}
track_low_thresh: {cfg.track_low_thresh}
new_track_thresh: {cfg.new_track_thresh}
track_buffer: {cfg.track_buffer}
match_thresh: {cfg.match_thresh}
fuse_score: {str(cfg.fuse_score).lower()}
proximity_thresh: 0.5
appearance_thresh: 0.25
with_reid: false
"""

    # Proje dizininde kalıcı dosya oluştur
    yaml_dir = Path(__file__).resolve().parent.parent / "tracker_configs"
    yaml_dir.mkdir(exist_ok=True)
    yaml_path = yaml_dir / f"custom_{cfg.tracker_type}.yaml"

    with open(yaml_path, "w") as f:
        f.write(yaml_content)

    print(f"[DETECTOR] Özel tracker YAML oluşturuldu: {yaml_path}")
    return str(yaml_path)


class EggDetector:
    """
    YOLO algılama + ByteTrack takip.

    Düzeltmeler:
      - Özel tracker YAML (parametreler gerçekten uygulanıyor)
      - Batch tensor dönüşümü (RPi5 hız)
      - track() ile warmup
    """

    def __init__(self, det_cfg: DetectorConfig, tracker_cfg: TrackerConfig):
        self.cfg = det_cfg
        self.tracker_cfg = tracker_cfg

        # Model yükle
        model_path = Path(det_cfg.model_path)
        if not model_path.exists():
            raise FileNotFoundError(f"Model bulunamadı: {model_path.absolute()}")

        from ultralytics import YOLO
        self.model = YOLO(str(model_path))

        # Özel tracker YAML oluştur
        if not tracker_cfg.custom_yaml_path:
            tracker_cfg.custom_yaml_path = _create_custom_tracker_yaml(tracker_cfg)

        self._tracker_yaml = tracker_cfg.custom_yaml_path

        # Video loop / reset sonrası ilk frame'de persist=False kullan
        # (boş trackers[] listesine erişim hatasını önler)
        self._first_after_reset: bool = False

        # Warmup (track ile, predict ile değil!)
        self._warmup()

    def _warmup(self):
        """Warmup: İlk frame gecikmesini önler. track() ile yapılmalı."""
        dummy = np.zeros((self.cfg.imgsz, self.cfg.imgsz, 3), dtype=np.uint8)
        try:
            self.model.track(
                source=dummy,
                imgsz=self.cfg.imgsz,
                conf=self.cfg.conf_threshold,
                iou=self.cfg.iou_threshold,
                device=self.cfg.device,
                half=self.cfg.half,
                persist=True,
                tracker=self._tracker_yaml,
                verbose=False,
            )
        except Exception:
            pass

    def detect_and_track(self, frame: np.ndarray, persist: bool = True):
        """
        Algılama + ByteTrack takip (tek çağrı).

        Returns:
            Ultralytics Results nesnesi
        """
        # reset_tracker() sonrası ilk frame: persist=False ile yeni tracker başlat.
        # persist=True + boş trackers[] listesi -> IndexError (Ultralytics bug workaround)
        actual_persist = persist
        if self._first_after_reset:
            actual_persist = False
            self._first_after_reset = False

        try:
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
                persist=actual_persist,
                tracker=self._tracker_yaml,
                verbose=False,
            )
            return results[0] if results else None
        except (IndexError, RuntimeError) as e:
            # Tracker yeniden başlatma sırasındaki geçici hata - sonraki frame normal
            return None

    @staticmethod
    def parse_results(result) -> list:
        """
        YOLO sonuçlarını standart dict listesine çevir.

        Düzeltme: Batch tensor dönüşümü (.cpu().numpy() tek seferde).
        Eski kod her algılama için ayrı ayrı yapıyordu -> N×overhead.
        """
        detections = []
        if result is None or result.boxes is None or len(result.boxes) == 0:
            return detections

        boxes = result.boxes

        # --- BATCH dönüşüm (eski: her i için ayrı .cpu().numpy()) ---
        xyxy_all = boxes.xyxy.cpu().numpy().astype(int)
        conf_all = boxes.conf.cpu().numpy()
        cls_all = boxes.cls.cpu().numpy().astype(int)

        has_ids = boxes.id is not None
        if has_ids:
            id_all = boxes.id.cpu().numpy().astype(int)

        names = result.names

        for i in range(len(boxes)):
            x1, y1, x2, y2 = xyxy_all[i]
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            cls_id = int(cls_all[i])

            detections.append({
                "track_id": int(id_all[i]) if has_ids else None,
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                "center": (int(cx), int(cy)),
                "confidence": round(float(conf_all[i]), 3),
                "class_id": cls_id,
                "class_name": names[cls_id] if names else "egg",
            })

        return detections

    def reset_tracker(self):
        """Tracker state'ini sıfırla (video loop veya reset için)."""
        if hasattr(self.model, 'predictor') and self.model.predictor is not None:
            if hasattr(self.model.predictor, 'trackers'):
                self.model.predictor.trackers = []
        # Bir sonraki detect_and_track çağrısında persist=False kullan
        self._first_after_reset = True
