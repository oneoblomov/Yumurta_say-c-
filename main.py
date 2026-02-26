#!/usr/bin/env python3
"""
main.py - Yumurta Sayıcı Giriş Noktası
=========================================
Endüstriyel gerçek zamanlı yumurta sayma sistemi.

Kullanım:
    # Varsayılan kamera (0) ile çalıştır
    python main.py

    # Video dosyası ile çalıştır
    python main.py --source video.mp4

    # Özel model ile çalıştır
    python main.py --model path/to/best.pt

    # GPU seçimi
    python main.py --device cuda:0

    # Tam parametre listesi
    python main.py --help

Kontrol tuşları:
    q / ESC  : Çıkış
    r        : Sayaç sıfırla
    d        : Debug modu aç/kapat
    +/-      : Sayım çizgisini yukarı/aşağı kaydır
    s        : Ekran görüntüsü kaydet
    f        : Tam ekran aç/kapat
    SPACE    : Durakla/Devam et
"""

import argparse
import sys
from pathlib import Path

# Proje kök dizinini path'e ekle
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from egg_counter.config import SystemConfig
from egg_counter.pipeline import EggCountingPipeline


def parse_args():
    """Komut satırı argümanlarını ayrıştır."""
    parser = argparse.ArgumentParser(
        description="Yumurta Sayıcı - Endüstriyel Gerçek Zamanlı Sayım Sistemi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Örnekler:
  python main.py                            # Varsayılan kamera
  python main.py --source 1                 # İkinci kamera
  python main.py --source video.mp4         # Video dosyası
  python main.py --conf 0.5 --debug         # Yüksek güven + debug
  python main.py --save-output out.mp4      # Çıktı kaydet
        """
    )

    # Video kaynağı
    parser.add_argument(
        "--source", type=str, default="0",
        help="Video kaynağı: kamera indeksi (0,1,..) veya dosya yolu (default: 0)"
    )

    # Model
    parser.add_argument(
        "--model", type=str,
        default="runs/detect/runs/train/egg_count/weights/best.pt",
        help="YOLO model yolu"
    )

    # Algılama
    parser.add_argument("--conf", type=float, default=0.45,
                        help="Güven eşiği (default: 0.45)")
    parser.add_argument("--iou", type=float, default=0.5,
                        help="NMS IoU eşiği (default: 0.5)")
    parser.add_argument("--imgsz", type=int, default=640,
                        help="Inference boyutu (default: 640)")

    # Cihaz
    parser.add_argument("--device", type=str, default="",
                        help="Cihaz: cuda:0, cpu (default: auto)")
    parser.add_argument("--no-half", action="store_true",
                        help="FP16 devre dışı bırak (FP32 kullan)")

    # Sayım çizgisi
    parser.add_argument("--line-pos", type=float, default=0.5,
                        help="Sayım çizgisi pozisyonu 0.0-1.0 (default: 0.5)")
    parser.add_argument("--direction", type=str, default="top_to_bottom",
                        choices=["top_to_bottom", "bottom_to_top", "both"],
                        help="Sayım yönü (default: top_to_bottom)")

    # Kamera
    parser.add_argument("--width", type=int, default=1280,
                        help="Kamera genişliği (default: 1280)")
    parser.add_argument("--height", type=int, default=720,
                        help="Kamera yüksekliği (default: 720)")

    # Performans
    parser.add_argument("--skip-frames", type=int, default=0,
                        help="Frame atlama (0=yok, 1=her 2.de, default: 0)")
    parser.add_argument("--tracker", type=str, default="bytetrack",
                        choices=["bytetrack", "botsort"],
                        help="Tracker tipi (default: bytetrack)")

    # Ön işleme
    parser.add_argument("--no-clahe", action="store_true",
                        help="CLAHE ön işleme devre dışı")
    parser.add_argument("--no-stabilize", action="store_true",
                        help="Titreşim stabilizasyonu devre dışı")

    # Çıktı
    parser.add_argument("--save-output", type=str, default="",
                        help="Çıktı video yolu (boş = kaydetme)")
    parser.add_argument("--fullscreen", action="store_true",
                        help="Tam ekran başlat")

    # Debug
    parser.add_argument("--debug", action="store_true",
                        help="Debug modunu aç")
    parser.add_argument("--no-trail", action="store_true",
                        help="İz çizgilerini gizle")

    return parser.parse_args()


def build_config(args) -> SystemConfig:
    """Argümanlardan SystemConfig oluştur."""
    config = SystemConfig()

    # Detector
    config.detector.model_path = args.model
    config.detector.conf_threshold = args.conf
    config.detector.iou_threshold = args.iou
    config.detector.imgsz = args.imgsz
    config.detector.device = args.device
    if args.no_half:
        config.detector.half = False

    # Tracker
    config.tracker.tracker_type = args.tracker

    # Counter
    config.counter.line_position = args.line_pos
    config.counter.direction = args.direction

    # Preprocessor
    config.preprocessor.enable_clahe = not args.no_clahe
    config.preprocessor.enable_stabilization = not args.no_stabilize

    # Pipeline
    config.pipeline.source = args.source
    config.pipeline.camera_width = args.width
    config.pipeline.camera_height = args.height
    config.pipeline.skip_frames = args.skip_frames
    config.pipeline.debug_mode = args.debug
    config.pipeline.show_track_trail = not args.no_trail
    config.pipeline.fullscreen = args.fullscreen

    if args.save_output:
        config.pipeline.save_output = True
        config.pipeline.output_path = args.save_output

    return config


def main():
    """Ana giriş noktası."""
    args = parse_args()
    config = build_config(args)

    # Sistem bilgisi
    print(f"\n{'='*60}")
    print(f"  Model    : {config.detector.model_path}")
    print(f"  Kaynak   : {config.pipeline.source}")
    print(f"  Cihaz    : {config.detector.device}")
    print(f"  ImgSz    : {config.detector.imgsz}")
    print(f"  Conf     : {config.detector.conf_threshold}")
    print(f"  Tracker  : {config.tracker.tracker_type}")
    print(f"  Çizgi    : {config.counter.line_position:.0%}")
    print(f"  Yön      : {config.counter.direction}")
    print(f"  CLAHE    : {'Açık' if config.preprocessor.enable_clahe else 'Kapalı'}")
    print(f"  Stabil.  : {'Açık' if config.preprocessor.enable_stabilization else 'Kapalı'}")
    print(f"{'='*60}\n")

    # Pipeline oluştur ve çalıştır
    pipeline = EggCountingPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()
