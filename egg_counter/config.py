"""
config.py - Merkezi Konfigürasyon Modülü
========================================
Raspberry Pi 5 optimize varsayılan değerler.
torch import'u lazy yapılır (RPi5'te gereksiz yükü azaltır).
"""

from dataclasses import dataclass, field
from typing import Tuple, Optional


def _detect_device() -> str:
    """GPU/CPU otomatik algılama (lazy import)."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda:0"
    except ImportError:
        pass
    return "cpu"


@dataclass
class DetectorConfig:
    """YOLO algılama parametreleri."""
    model_path: str = "models/yolo26s_mod/best.pt"
    imgsz: int = 480                      # RPi5: 480 dengeli (640 ağır, 320 düşük kalite)
    conf_threshold: float = 0.30          # DÜŞÜK: %99.9 recall için kaçırılan yumurta olmasın
    iou_threshold: float = 0.45           # NMS IoU (sıkışık yumurtalar için)
    max_det: int = 200
    device: str = ""                      # "" = auto
    half: bool = False                    # RPi5 CPU -> FP16 yok
    agnostic_nms: bool = True             # Tek sınıf -> agnostic NMS daha iyi
    augment: bool = False
    classes: Optional[list] = None
    vid_stride: int = 1

    def __post_init__(self):
        if not self.device:
            self.device = _detect_device()
        if "cpu" in self.device:
            self.half = False

@dataclass
class TrackerConfig:
    """
    ByteTrack takip parametreleri.
    Bu değerler özel YAML dosyasına yazılır; varsayılan bytetrack.yaml KULLANILMAZ.
    """
    tracker_type: str = "bytetrack"

    # ByteTrack parametreleri (custom YAML'a yazılacak)
    track_high_thresh: float = 0.35       # Düşük: ID kaybını azaltır
    track_low_thresh: float = 0.05        # Çok düşük: 2. aşamada bile eşleş
    new_track_thresh: float = 0.40        # Yeni ID başlatma eşiği
    track_buffer: int = 90                # 90 frame (~3sn) kayıp toleransı
    match_thresh: float = 0.85            # Katı IoU eşleşme
    fuse_score: bool = True

    custom_yaml_path: str = ""            # Otomatik oluşturulur


@dataclass
class CounterConfig:
    """Sayım çizgisi parametreleri."""
    line_position: float = 0.5
    line_thickness: int = 2
    line_color: Tuple[int, int, int] = (0, 255, 255)
    direction: str = "top_to_bottom"
    crossing_margin: int = 8              # Geçiş toleransı (px)
    double_count_cooldown: int = 30       # Frame cooldown (çift sayım engelleyici)
    min_track_age: int = 2                # Hızlı bant için 2 frame yeterli
    spatial_dedup_radius: int = 40        # Aynı bölgedeki yeni ID -> eski olarak kabul et (px)

    # Çizgi geçişi sonrası takibi bırak
    # 0 = devre dışı (her zaman takip et)
    # >0 = sayıldıktan bu kadar frame sonra trail + görselleme bırakılır (FPS kazanımı)
    post_cross_drop_frames: int = 0

    # ROI (İlgi Alanı) sınırları – 3-çizgi sistemi
    # Sadece bu bant içinde kalan nesneler YOLO'ya gönderilir (FPS kazanımı).
    # Üst ROI çizgisi: line_position'dan yukarıya (default %15 üstte)
    # Alt ROI çizgisi: line_position'dan aşağıya (default %15 altta)
    roi_top_position: float = 0.35        # Üst ROI sınırı (0-1 arası)
    roi_bottom_position: float = 0.65     # Alt ROI sınırı (0-1 arası)
    roi_line_color: Tuple[int, int, int] = (0, 165, 255)   # ROI çizgi rengi (turuncu)
    roi_line_thickness: int = 1


@dataclass
class VisualizerConfig:
    """Görsel işaretleme parametreleri (RPi5 optimize)."""
    uncounted_color: Tuple[int, int, int] = (0, 0, 255)
    uncounted_alpha: float = 0.30
    counted_color: Tuple[int, int, int] = (0, 255, 0)
    counted_alpha: float = 0.30

    font_scale: float = 0.45
    font_thickness: int = 1
    label_bg_alpha: float = 0.6

    hud_font_scale: float = 0.6
    hud_color: Tuple[int, int, int] = (255, 255, 255)
    hud_bg_color: Tuple[int, int, int] = (40, 40, 40)
    hud_bg_alpha: float = 0.7

    show_bbox: bool = True
    bbox_thickness: int = 2
    enable_label_bg: bool = False         # RPi5: Kapalı (her etiket için frame.copy())
    enable_count_flash: bool = True       # Sayıldığında +1 efekti

    headless: bool = False                # Web arayüzü için HUD gizle


@dataclass
class PreprocessorConfig:
    """RPi5 hafif ön işleme. Stabilizasyon ve denoise varsayılan KAPALI."""
    enable_clahe: bool = True
    clahe_clip_limit: float = 2.5
    clahe_grid_size: Tuple[int, int] = (4, 4)  # 4x4 daha hızlı

    enable_denoise: bool = False          # ~50ms/frame, RPi5'te kullanma
    denoise_strength: int = 3

    enable_stabilization: bool = False    # ~15ms/frame, RPi5'te kullanma
    stabilization_smoothing: int = 5

    adaptive_brightness: bool = True
    brightness_check_interval: int = 5    # Her 5 frame'de bir (her frame değil)


@dataclass
class LoggerConfig:
    """Log sistemi parametreleri."""
    log_dir: str = "logs"
    csv_prefix: str = "count_events"
    daily_prefix: str = "daily_total"
    enable_csv_log: bool = True
    enable_daily_total: bool = True
    flush_interval: int = 1               # Her sayımda flush (endüstriyel: veri kaybı 0)


@dataclass
class PipelineConfig:
    """Ana pipeline konfigürasyonu (RPi5 optimize)."""
    source: str = "0"
    camera_width: int = 640               # RPi5: 640x480
    camera_height: int = 480
    camera_fps: int = 30
    camera_backend: str = "auto"          # auto | v4l2

    # Kenar kırpma – FPS artırmak için (küçük frame -> hızlı inference)
    # crop_ud: üstten ve alttan TOPLAM yüzde (örn. 20 -> her yandan %10)
    # crop_lr: soldan ve sağdan TOPLAM yüzde (örn. 20 -> her yandan %10)
    crop_ud: int = 0                      # 0 = kırpma yok (default)
    crop_lr: int = 0                      # 0 = kırpma yok (default)

    target_fps: int = 25
    skip_frames: int = 0
    buffer_size: int = 1                  # 1 = minimum gecikme
    use_threaded_capture: bool = True     # Ayrı thread ile kamera okuma

    debug_mode: bool = False
    show_fps: bool = True
    show_track_trail: bool = True
    trail_length: int = 20

    save_output: bool = False
    output_path: str = "output.mp4"
    output_codec: str = "mp4v"
    headless: bool = False                # Ekransız çalışma (RPi5 server modu)

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
        from dataclasses import asdict
        return asdict(self)
