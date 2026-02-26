#!/usr/bin/env python3
"""
main.py - Yumurta Sayıcı v2.0 - RPi5 Optimize
=================================================
Endüstriyel gerçek zamanlı yumurta sayma sistemi.

Kullanım:
    python main.py                              # Varsayılan kamera
    python main.py --source video.mp4           # Video dosyası
    python main.py --model best.pt --imgsz 480  # Özel model
    python main.py --headless                   # Ekransız (RPi5 server)
    python main.py --help                       # Tüm parametreler

Kontrol tuşları:
    q / ESC  : Çıkış
    r        : Sayaç sıfırla
    d        : Debug modu aç/kapat
    +/-      : Sayım çizgisini kaydır
    s        : Ekran görüntüsü kaydet
    f        : Tam ekran aç/kapat
    SPACE    : Durakla/Devam et
"""

import argparse
import sys
import os
from pathlib import Path

# FFmpeg çok iş parçacıklı çözücüyü devre dışı bırak.
# ThreadedCapture + FFmpeg dâhilî thread'leri çakışır:
#   "Assertion fctx->async_lock failed at libavcodec/pthread_frame.c"
# OPENCV_FFMPEG_CAPTURE_OPTIONS env var, cv2 import'undan ÖNCE ayarlanmalı.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "threads;1|fflags;nobuffer")
os.environ.setdefault("OPENCV_FFMPEG_MULTITHREADED", "0")  # eski OpenCV versiyonları için

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from egg_counter.config import SystemConfig
from egg_counter.pipeline import EggCountingPipeline


def parse_args():
    parser = argparse.ArgumentParser(
        description="Yumurta Sayıcı v2.0 - RPi5 Optimize Endüstriyel Sayım",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Örnekler:
  python main.py                                # Varsayılan kamera (640x480)
  python main.py --source video.mp4             # Video dosyası
  python main.py --conf 0.25 --debug            # Düşük güven + debug
  python main.py --imgsz 320                    # Hızlı mod (düşük çözünürlük)
  python main.py --headless --save-output o.mp4 # Ekransız kayıt
  python main.py --no-threaded                  # Thread'siz kamera
        """
    )

    # Video kaynağı
    parser.add_argument("--source", type=str, default="0",
                        help="Kamera indeksi veya video dosya yolu (default: 0)")

    # Model
    parser.add_argument("--model", type=str,
                        default="models/yolo26s_mod/best.pt",
                        help="YOLO model yolu (.pt, .onnx, .ncnn)")

    # Algılama
    parser.add_argument("--conf", type=float, default=0.30,
                        help="Güven eşiği (default: 0.30 - yüksek recall)")
    parser.add_argument("--iou", type=float, default=0.45,
                        help="NMS IoU eşiği (default: 0.45)")
    parser.add_argument("--imgsz", type=int, default=480,
                        help="Inference boyutu (default: 480, RPi5 dengeli)")

    # Cihaz
    parser.add_argument("--device", type=str, default="",
                        help="Cihaz: cuda:0, cpu (default: auto)")

    # Sayım çizgisi
    parser.add_argument("--line-pos", type=float, default=0.5,
                        help="Sayım çizgisi pozisyonu 0.0-1.0 (default: 0.5)")
    parser.add_argument("--direction", type=str, default="top_to_bottom",
                        choices=["top_to_bottom", "bottom_to_top", "both"],
                        help="Sayım yönü (default: top_to_bottom)")

    # Kamera
    parser.add_argument("--width", type=int, default=640,
                        help="Kamera genişliği (default: 640)")
    parser.add_argument("--height", type=int, default=480,
                        help="Kamera yüksekliği (default: 480)")

    # Performans
    parser.add_argument("--skip-frames", type=int, default=0,
                        help="Frame atlama (default: 0)")
    parser.add_argument("--tracker", type=str, default="bytetrack",
                        choices=["bytetrack", "botsort"],
                        help="Tracker tipi (default: bytetrack)")
    parser.add_argument("--no-threaded", action="store_true",
                        help="Thread'li kamera yakalamayı kapat")

    # Tracker hassasiyeti
    parser.add_argument("--track-buffer", type=int, default=90,
                        help="Kayıp ID buffer (frame) (default: 90)")
    parser.add_argument("--match-thresh", type=float, default=0.85,
                        help="Tracker IoU eşleştirme eşiği (default: 0.85)")

    # Ön işleme
    parser.add_argument("--no-clahe", action="store_true",
                        help="CLAHE ön işleme devre dışı")
    parser.add_argument("--stabilize", action="store_true",
                        help="Titreşim stabilizasyonu AÇ (RPi5'te varsayılan kapalı)")

    # Çıktı
    parser.add_argument("--save-output", type=str, default="",
                        help="Çıktı video yolu")
    parser.add_argument("--fullscreen", action="store_true",
                        help="Tam ekran başlat")
    parser.add_argument("--headless", action="store_true",
                        help="Ekransız çalışma (RPi5 server modu)")

    # Debug
    parser.add_argument("--debug", action="store_true",
                        help="Debug modunu aç")
    parser.add_argument("--no-trail", action="store_true",
                        help="İz çizgilerini gizle")

    return parser.parse_args()


def build_config(args) -> SystemConfig:
    config = SystemConfig()

    # Detector
    config.detector.model_path = args.model
    config.detector.conf_threshold = args.conf
    config.detector.iou_threshold = args.iou
    config.detector.imgsz = args.imgsz
    if args.device:
        config.detector.device = args.device

    # Tracker
    config.tracker.tracker_type = args.tracker
    config.tracker.track_buffer = args.track_buffer
    config.tracker.match_thresh = args.match_thresh

    # Counter
    config.counter.line_position = args.line_pos
    config.counter.direction = args.direction

    # Preprocessor
    config.preprocessor.enable_clahe = not args.no_clahe
    config.preprocessor.enable_stabilization = args.stabilize  # Varsayılan KAPALI

    # Pipeline
    config.pipeline.source = args.source
    config.pipeline.camera_width = args.width
    config.pipeline.camera_height = args.height
    config.pipeline.skip_frames = args.skip_frames
    config.pipeline.debug_mode = args.debug
    config.pipeline.show_track_trail = not args.no_trail
    config.pipeline.fullscreen = args.fullscreen
    config.pipeline.headless = args.headless
    config.pipeline.use_threaded_capture = not args.no_threaded

    if args.save_output:
        config.pipeline.save_output = True
        config.pipeline.output_path = args.save_output

    return config


def main():
    args = parse_args()
    config = build_config(args)

    print(f"\n{'='*60}")
    print(f"  YUMURTA SAYICI v2.0 - RPi5 Optimize")
    print(f"{'='*60}")
    print(f"  Model      : {config.detector.model_path}")
    print(f"  Kaynak     : {config.pipeline.source}")
    print(f"  Cihaz      : {config.detector.device}")
    print(f"  ImgSz      : {config.detector.imgsz}")
    print(f"  Conf       : {config.detector.conf_threshold}")
    print(f"  IoU        : {config.detector.iou_threshold}")
    print(f"  Tracker    : {config.tracker.tracker_type}")
    print(f"  TrackBuf   : {config.tracker.track_buffer} frame")
    print(f"  MatchThr   : {config.tracker.match_thresh}")
    print(f"  Çizgi      : {config.counter.line_position:.0%}")
    print(f"  Yön        : {config.counter.direction}")
    print(f"  Kamera     : {config.pipeline.camera_width}x{config.pipeline.camera_height}")
    print(f"  CLAHE      : {'Açık' if config.preprocessor.enable_clahe else 'Kapalı'}")
    print(f"  Stabil.    : {'Açık' if config.preprocessor.enable_stabilization else 'Kapalı'}")
    print(f"  Threaded   : {'Açık' if config.pipeline.use_threaded_capture else 'Kapalı'}")
    print(f"  Headless   : {'Açık' if config.pipeline.headless else 'Kapalı'}")
    print(f"{'='*60}\n")

    pipeline = EggCountingPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()
