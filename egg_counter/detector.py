"""
detector.py - YOLO Algılama + Takip Modülü
============================================
Kritik düzeltmeler:
  1. Özel ByteTrack YAML oluşturuluyor (TrackerConfig parametreleri gerçekten uygulanıyor)
  2. parse_results() batch tensor dönüşümü (RPi5 hız)
  3. Warmup track() ile yapılıyor (predict ile değil)
  4. NCNN/ONNX model desteği eklendi
"""
import numpy as np
from pathlib import Path

from .config import DetectorConfig, TrackerConfig


def _create_custom_tracker_yaml(cfg: TrackerConfig) -> str:
    """
    TrackerConfig'den özel ByteTrack/BotSORT YAML dosyası oluştur.

    ESKİ HATA: Varsayılan bytetrack.yaml kullanılıyordu, tüm özel
    parametreler (track_buffer, match_thresh vb.) yok sayılıyordu.
    Bu fonksiyon parametreleri gerçek YAML'a yazar.

    tracker_type destekleri:
      - bytetrack : Standart hızlı ByteTrack
      - botsort   : Global hareket düzeltme + GMC (titreşim toleranslı)
      - dense     : Bitişik/yakın yumurtalar için optimize ByteTrack
                    (düşük eşikler, yüksek buffer, gevşek eşleşme)

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
    elif cfg.tracker_type == "dense":
        # Bitişik/yakın yumurtalar için özel ByteTrack:
        # - Düşük high_thresh: kısmen örtüşen yumurtaları kaçırma
        # - Çok düşük low_thresh: ikinci aşama eşleşme spektrumu geniş
        # - Gevşek match_thresh: yumurtalar birbirini geçince IoU düşer, yine de eşleş
        # - Yüksek buffer: geçici okluzyondan sonra ID kaybetme
        yaml_content = f"""# Yoğun/Bitişik Yumurta - Optimize ByteTrack - Yumurta Sayıcı
tracker_type: bytetrack
track_high_thresh: 0.25
track_low_thresh: 0.03
new_track_thresh: 0.30
track_buffer: {max(cfg.track_buffer, 120)}
match_thresh: 0.70
fuse_score: true
"""
    else:  # botsort
        # BotSORT: gmc_method ZORUNLU, fuse_score ByteTrack'e özgüdür.
        # track_buffer: 0 → track'ler anında ölür; minimum 30 frame zorunlu.
        # gmc_method: sparseOptFlow → RPi5'te ağır; 'orb' daha hafif,
        #   tamamen devre dışı için 'sof' kullan.
        safe_buffer = max(cfg.track_buffer, 30)  # hiçbir zaman 0 olmamalı
        yaml_content = f"""# Özel BotSORT konfigürasyonu - Yumurta Sayıcı
# NOT: BotSORT = ByteTrack + Global Motion Compensation (GMC)
# fuse_score BotSORT'ta dikkate ALINMAZ ama bazı Ultralytics sürümleri bekler.
tracker_type: botsort
track_high_thresh: {cfg.track_high_thresh}
track_low_thresh: {cfg.track_low_thresh}
new_track_thresh: {cfg.new_track_thresh}
track_buffer: {safe_buffer}
match_thresh: {cfg.match_thresh}
fuse_score: false
proximity_thresh: 0.5
appearance_thresh: 0.25
with_reid: false
gmc_method: orb
"""

    # Proje dizininde kalıcı dosya oluştur
    yaml_dir = Path(__file__).resolve().parent.parent / "tracker_configs"
    yaml_dir.mkdir(exist_ok=True)
    # "dense" tipi de bytetrack tabanlı olduğu için ayrı dosya adı
    safe_name = cfg.tracker_type.replace(" ", "_")
    yaml_path = yaml_dir / f"custom_{safe_name}.yaml"

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

    def detect_and_track(self, frame_orig: np.ndarray, persist: bool = True):
        """
        Algılama + ByteTrack takip (tek çağrı).
        İsteğe bağlı CLAHE ve Stabilizasyon ön işlemleri ekli.

        Returns:
            Ultralytics Results nesnesi
        """
        frame = frame_orig
        
        # Ön İşleme: CLAHE (Kontrast Artırma)
        if self.cfg.enable_clahe:
            try:
                import cv2
                lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
                l, a, b = cv2.split(lab)
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
                cl = clahe.apply(l)
                limg = cv2.merge((cl,a,b))
                frame = cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)
            except Exception:
                pass

        # reset_tracker() sonrası ilk frame: persist=False ile yeni tracker başlat.
        # persist=True + boş trackers[] listesi -> IndexError (Ultralytics bug workaround)
        actual_persist = persist
        if self._first_after_reset:
            actual_persist = False
            self._first_after_reset = False

        # hazırlık: tracker hata bayrağı yoksa oluştur
        if not hasattr(self, '_tracker_broken'):
            self._tracker_broken = False

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
        except IndexError as e:
            # Ultralytics bug: iç trackers[] listesi bozulunca sürekli IndexError.
            # persist=False ile sadece bu frame'i kurtarmak yetmiyor —
            # bir sonraki frame persist=True ile gelince aynı hata tekrar oluşur.
            # ÇÖZÜM: tracker state'ini tamamen temizle, sonraki frame persist=False
            # ile başlasın (reset_tracker'ın yaptığının aynısı).
            tracker_type = self.tracker_cfg.tracker_type
            # sadece ilk hata mesajını logla, sonra sessiz kal
            if not getattr(self, '_tracker_broken', False):
                print(f"[DETECTOR] {tracker_type} tracker bozuldu, sıfırlanıyor: {e}")
            self._tracker_broken = True
            # İç state temizle
            if hasattr(self.model, 'predictor') and self.model.predictor is not None:
                if hasattr(self.model.predictor, 'trackers'):
                    self.model.predictor.trackers = []
            self._first_after_reset = True  # sonraki frame persist=False kullanacak
            # Bu frame'i persist=False ile çalıştır (ID'siz detection döner, OK)
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
                    persist=False,
                    tracker=self._tracker_yaml,
                    verbose=False,
                )
                return results[0] if results else None
            except Exception:
                return None
        except Exception as e:
            # Diğer geçici hatalar
            tracker_type = self.tracker_cfg.tracker_type
            print(f"[DETECTOR] {tracker_type} tracker hatası (atlanıyor): {type(e).__name__}: {e}")
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
        # hata bayrağını temizle, yeni döngüde log tekrar çıkabilir
        self._tracker_broken = False
