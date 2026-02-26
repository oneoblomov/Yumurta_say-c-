"""
config.py - Merkezi Konfigürasyon Modülü
========================================
Tüm sistem parametreleri tek bir yerden yönetilir.
Dataclass tabanlı, tip güvenli, doğrulama destekli.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple, Optional
import torch


@dataclass
class DetectorConfig:
    """YOLO algılama parametreleri."""
    model_path: str = "runs/detect/runs/train/egg_count/weights/best.pt"
    imgsz: int = 640
    conf_threshold: float = 0.45          # Güven eşiği (zorlu koşullar için dengelenmiş)
    iou_threshold: float = 0.5            # NMS IoU eşiği
    max_det: int = 300                    # Maks algılama sayısı / frame
    device: str = ""                      # "" = auto (GPU varsa GPU)
    half: bool = True                     # FP16 (GPU performansı için)
    agnostic_nms: bool = False            # Sınıf-agnostik NMS
    augment: bool = False                 # Test-time augmentation (TTA)
    classes: Optional[list] = None        # Filtrelenecek sınıflar (None = hepsi)

    def __post_init__(self):
        if not self.device:
            self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        if self.device == "cpu":
            self.half = False  # CPU'da FP16 desteklenmez


@dataclass
class TrackerConfig:
    """ByteTrack takip parametreleri."""
    tracker_type: str = "bytetrack"       # bytetrack | botsort
    track_high_thresh: float = 0.5        # Yüksek güven eşiği (1. aşama eşleştirme)
    track_low_thresh: float = 0.1         # Düşük güven eşiği (2. aşama eşleştirme)
    new_track_thresh: float = 0.6         # Yeni iz başlatma eşiği
    track_buffer: int = 60                # Kayıp iz tamponu (frame) - titreşim dayanıklılığı
    match_thresh: float = 0.8             # IoU eşleştirme eşiği
    fuse_score: bool = True               # Algılama skoru füzyonu


@dataclass
class CounterConfig:
    """Sayım çizgisi parametreleri."""
    line_position: float = 0.5            # Ekran yüksekliğinin oranı (0.0-1.0)
    line_thickness: int = 2               # Çizgi kalınlığı (px)
    line_color: Tuple[int, int, int] = (0, 255, 255)  # Sarı (BGR)
    direction: str = "top_to_bottom"      # Sayım yönü: top_to_bottom | bottom_to_top | both
    crossing_margin: int = 5              # Çizgi geçiş toleransı (px) - titreşim koruması
    double_count_cooldown: int = 15       # Aynı ID için yeniden sayım engelleme (frame)
    min_track_age: int = 3                # Sayılmadan önce minimum takip süresi (frame)


@dataclass
class VisualizerConfig:
    """Görsel işaretleme parametreleri."""
    # Sayılmamış yumurta (kırmızı)
    uncounted_color: Tuple[int, int, int] = (0, 0, 255)     # BGR kırmızı
    uncounted_alpha: float = 0.35                             # Yarı saydamlık

    # Sayılmış yumurta (yeşil)
    counted_color: Tuple[int, int, int] = (0, 255, 0)        # BGR yeşil
    counted_alpha: float = 0.35                               # Yarı saydamlık

    # Etiket
    font_scale: float = 0.5
    font_thickness: int = 1
    label_bg_alpha: float = 0.7

    # HUD (Head-Up Display)
    hud_font_scale: float = 0.7
    hud_color: Tuple[int, int, int] = (255, 255, 255)
    hud_bg_color: Tuple[int, int, int] = (40, 40, 40)
    hud_bg_alpha: float = 0.8

    # Bounding box
    show_bbox: bool = True
    bbox_thickness: int = 2


@dataclass
class PreprocessorConfig:
    """Adaptif ön işleme parametreleri."""
    enable_clahe: bool = True             # Adaptif histogram eşitleme (değişken ışık)
    clahe_clip_limit: float = 2.0
    clahe_grid_size: Tuple[int, int] = (8, 8)

    enable_denoise: bool = False          # Gürültü azaltma (toz ortamları için)
    denoise_strength: int = 5

    enable_stabilization: bool = True     # Kamera titreşim stabilizasyonu
    stabilization_smoothing: int = 5      # Smoothing penceresi

    adaptive_brightness: bool = True      # Ani ışık değişimine adaptif


@dataclass
class LoggerConfig:
    """Log sistemi parametreleri."""
    log_dir: str = "logs"
    csv_prefix: str = "count_events"
    daily_prefix: str = "daily_total"
    enable_csv_log: bool = True
    enable_daily_total: bool = True
    flush_interval: int = 10              # Her N sayımda diske yaz


@dataclass
class PipelineConfig:
    """Ana pipeline konfigürasyonu."""
    # Video kaynağı
    source: str = "0"                     # Kamera indeksi veya video yolu
    camera_width: int = 1280
    camera_height: int = 720
    camera_fps: int = 30

    # Performans
    target_fps: int = 30
    skip_frames: int = 0                  # Her N frame'de bir algılama (0 = her frame)
    buffer_size: int = 2                  # Kamera buffer boyutu

    # Debug
    debug_mode: bool = False
    show_fps: bool = True
    show_track_trail: bool = True
    trail_length: int = 30                # İz uzunluğu (frame)

    # Kontrol
    save_output: bool = False
    output_path: str = "output.mp4"
    output_codec: str = "mp4v"

    # Pencere
    window_name: str = "Yumurta Sayici - Azim Tav"
    fullscreen: bool = False


@dataclass
class SystemConfig:
    """Tüm konfigürasyonları birleştiren üst konfigürasyon."""
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    counter: CounterConfig = field(default_factory=CounterConfig)
    visualizer: VisualizerConfig = field(default_factory=VisualizerConfig)
    preprocessor: PreprocessorConfig = field(default_factory=PreprocessorConfig)
    logger: LoggerConfig = field(default_factory=LoggerConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)

    @classmethod
    def from_dict(cls, d: dict) -> "SystemConfig":
        """Dict'ten konfigürasyon oluştur."""
        return cls(
            detector=DetectorConfig(**d.get("detector", {})),
            tracker=TrackerConfig(**d.get("tracker", {})),
            counter=CounterConfig(**d.get("counter", {})),
            visualizer=VisualizerConfig(**d.get("visualizer", {})),
            preprocessor=PreprocessorConfig(**d.get("preprocessor", {})),
            logger=LoggerConfig(**d.get("logger", {})),
            pipeline=PipelineConfig(**d.get("pipeline", {})),
        )

    def to_dict(self) -> dict:
        """Konfigürasyonu dict'e dönüştür."""
        from dataclasses import asdict
        return asdict(self)
