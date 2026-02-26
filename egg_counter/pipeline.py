"""
pipeline.py - Ana Orkestratör Modülü
======================================
Tüm modülleri birleştiren gerçek zamanlı pipeline.

Akış:
  Kamera → Ön İşleme → YOLO Algılama + ByteTrack → Track Yönetimi
  → Çizgi Kesişim → Sayım → Log → Görselleştirme → Ekran

Kontrol tuşları:
  q / ESC  : Çıkış
  r        : Sayaç sıfırla
  d        : Debug modu aç/kapat
  +/-      : Sayım çizgisini yukarı/aşağı kaydır
  s        : Ekran görüntüsü kaydet
  f        : Tam ekran aç/kapat
  SPACE    : Durakla/Devam et
"""

import cv2
import time
import numpy as np
from pathlib import Path
from typing import Optional

from .config import SystemConfig
from .preprocessor import FramePreprocessor
from .detector import EggDetector
from .tracker import TrackManager
from .counter import CountingLine
from .visualizer import Visualizer
from .logger import CountLogger


class EggCountingPipeline:
    """
    Endüstriyel gerçek zamanlı yumurta sayma pipeline'ı.

    Tüm modülleri orkestre eder:
      1. FramePreprocessor - Adaptif ön işleme
      2. EggDetector       - YOLO + ByteTrack
      3. TrackManager      - İz yönetimi
      4. CountingLine      - Sanal çizgi sayım
      5. Visualizer        - Görsel işaretleme
      6. CountLogger       - Log sistemi

    Kullanım:
        config = SystemConfig()
        pipeline = EggCountingPipeline(config)
        pipeline.run()
    """

    def __init__(self, config: SystemConfig):
        self.cfg = config

        # Durum
        self._running = False
        self._paused = False
        self._debug_mode = config.pipeline.debug_mode

        # FPS hesaplama
        self._fps = 0.0
        self._frame_times = []
        self._fps_update_interval = 10  # Her N frame'de bir FPS güncelle

        # Modüller (kamera açıldıktan sonra bazıları init edilecek)
        self._preprocessor: Optional[FramePreprocessor] = None
        self._detector: Optional[EggDetector] = None
        self._track_manager: Optional[TrackManager] = None
        self._counting_line: Optional[CountingLine] = None
        self._visualizer: Optional[Visualizer] = None
        self._logger: Optional[CountLogger] = None

        # Video yakalama
        self._cap: Optional[cv2.VideoCapture] = None
        self._writer: Optional[cv2.VideoWriter] = None
        self._frame_width = 0
        self._frame_height = 0

    def _init_camera(self) -> bool:
        """
        Kamera / video kaynağını başlat.

        Returns:
            Başarılı mı?
        """
        source = self.cfg.pipeline.source

        # Kamera indeksi mi, dosya yolu mu?
        if source.isdigit():
            source = int(source)

        self._cap = cv2.VideoCapture(source)

        if not self._cap.isOpened():
            print(f"[PIPELINE] HATA: Video kaynağı açılamadı: {self.cfg.pipeline.source}")
            return False

        # Kamera parametreleri ayarla (sadece gerçek kamera için)
        if isinstance(source, int):
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.pipeline.camera_width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.pipeline.camera_height)
            self._cap.set(cv2.CAP_PROP_FPS, self.cfg.pipeline.camera_fps)
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, self.cfg.pipeline.buffer_size)

        # Gerçek çözünürlüğü oku
        self._frame_width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._frame_height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        print(f"[PIPELINE] Kamera açıldı: {self._frame_width}x{self._frame_height}")
        return True

    def _init_modules(self):
        """Tüm alt modülleri başlat."""
        print("[PIPELINE] Modüller başlatılıyor...")

        # 1. Ön işlemci
        self._preprocessor = FramePreprocessor(self.cfg.preprocessor)

        # 2. Algılayıcı + Takip
        print("[PIPELINE] YOLO model yükleniyor...")
        self._detector = EggDetector(self.cfg.detector, self.cfg.tracker)

        # 3. Track yöneticisi
        self._track_manager = TrackManager(
            self.cfg.tracker, self.cfg.counter,
            trail_length=self.cfg.pipeline.trail_length
        )

        # 4. Sayım çizgisi
        self._counting_line = CountingLine(self.cfg.counter, self._frame_height)

        # 5. Görselleştirici
        self._visualizer = Visualizer(self.cfg.visualizer, self.cfg.counter)

        # 6. Logger
        self._logger = CountLogger(self.cfg.logger)

        # Logger callback'ini sayım çizgisine bağla
        self._counting_line.on_count(self._on_egg_counted)

        print("[PIPELINE] Tüm modüller hazır.")
        print(f"[PIPELINE] Sayım çizgisi: y={self._counting_line.line_y} "
              f"(frame_h={self._frame_height})")
        print(f"[PIPELINE] Cihaz: {self.cfg.detector.device}")

    def _init_video_writer(self):
        """Video yazıcı başlat (opsiyonel)."""
        if not self.cfg.pipeline.save_output:
            return

        fourcc = cv2.VideoWriter_fourcc(*self.cfg.pipeline.output_codec)
        fps = self.cfg.pipeline.target_fps
        self._writer = cv2.VideoWriter(
            self.cfg.pipeline.output_path,
            fourcc, fps,
            (self._frame_width, self._frame_height)
        )
        print(f"[PIPELINE] Video kayıt: {self.cfg.pipeline.output_path}")

    def run(self):
        """
        Ana çalışma döngüsü.
        Kamerayı başlatır, modülleri init eder, frame loop'u çalıştırır.
        """
        print("=" * 60)
        print("  YUMURTA SAYICI - Endüstriyel Üretim Bandı Sistemi")
        print("  Azim-Tav | v1.0")
        print("=" * 60)

        # 1. Kamera başlat
        if not self._init_camera():
            return

        # 2. Modülleri başlat
        self._init_modules()

        # 3. Video yazıcı (opsiyonel)
        self._init_video_writer()

        # 4. Pencere oluştur
        window_name = self.cfg.pipeline.window_name
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        if self.cfg.pipeline.fullscreen:
            cv2.setWindowProperty(
                window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN
            )

        self._running = True
        frame_count = 0
        skip = self.cfg.pipeline.skip_frames

        print("\n[PIPELINE] Çalışıyor... (q: çıkış, r: sıfırla, d: debug)")

        try:
            while self._running:
                loop_start = time.perf_counter()

                # Tuş kontrolü
                key = cv2.waitKey(1) & 0xFF
                self._handle_key(key)

                if self._paused:
                    continue

                # Frame oku
                ret, frame = self._cap.read()
                if not ret:
                    # Video dosyası bitti -> başa sar veya çık
                    if isinstance(self.cfg.pipeline.source, str) and \
                       not self.cfg.pipeline.source.isdigit():
                        self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    else:
                        print("[PIPELINE] Kamera bağlantısı kesildi!")
                        break

                frame_count += 1

                # Frame skip (performans optimizasyonu)
                if skip > 0 and frame_count % (skip + 1) != 0:
                    continue

                # === ANA PIPELINE ===
                display_frame = self._process_frame(frame)

                # Video kayıt
                if self._writer is not None:
                    self._writer.write(display_frame)

                # Ekrana göster
                cv2.imshow(window_name, display_frame)

                # FPS hesapla
                elapsed = time.perf_counter() - loop_start
                self._frame_times.append(elapsed)
                if len(self._frame_times) >= self._fps_update_interval:
                    avg_time = sum(self._frame_times) / len(self._frame_times)
                    self._fps = 1.0 / max(avg_time, 1e-6)
                    self._frame_times.clear()

        except KeyboardInterrupt:
            print("\n[PIPELINE] Kullanıcı tarafından durduruldu.")
        finally:
            self._cleanup()

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        """
        Tek bir frame'i tüm pipeline'dan geçir.

        Args:
            frame: Ham BGR frame

        Returns:
            İşaretlenmiş frame
        """
        # 1. Ön işleme (CLAHE, stabilizasyon, parlaklık)
        processed = self._preprocessor.process(frame)

        # 2. YOLO Algılama + ByteTrack Takip
        result = self._detector.detect_and_track(processed)
        detections = self._detector.parse_results(result)

        # 3. Track yönetimi (trail, age, durum)
        enriched = self._track_manager.update(detections)

        # 4. Sayım çizgisi geçiş kontrolü
        newly_counted = self._counting_line.check_crossings(
            enriched, self._track_manager
        )

        # 5. Trail verilerini topla (görselleştirme için)
        trails = {}
        if self.cfg.pipeline.show_track_trail:
            for det in enriched:
                tid = det.get("track_id")
                if tid is not None:
                    trails[tid] = self._track_manager.get_trail(tid)

        # 6. Görselleştir
        display = self._visualizer.draw(
            frame=frame,  # Orijinal frame üzerine çiz (ön işlenmiş değil)
            detections=enriched,
            counting_line_y=self._counting_line.line_y,
            total_count=self._counting_line.total_count,
            active_tracks=self._track_manager.get_active_count(),
            fps=self._fps,
            frame_width=self._frame_width,
            trails=trails,
            debug_mode=self._debug_mode,
            show_trails=self.cfg.pipeline.show_track_trail,
            newly_counted=newly_counted,
        )

        # 7. Debug bilgisi
        if self._debug_mode:
            debug_info = {
                "Frame": self._track_manager.frame_count,
                "Detections": len(detections),
                "Tracked": len([d for d in enriched if d.get("track_id")]),
                "Counted IDs": len(self._track_manager.counted_ids),
                "Line Y": self._counting_line.line_y,
                "Conf Thresh": self.cfg.detector.conf_threshold,
            }
            self._visualizer.draw_debug_info(display, debug_info)

        return display

    def _on_egg_counted(self, event: dict):
        """Sayım callback - logger'a ilet."""
        if self._logger:
            self._logger.log_count_event(event)

        # Konsol bildirimi
        tid = event.get("track_id", "?")
        total = event.get("total", 0)
        print(f"[SAYIM] ID #{tid} sayıldı → Toplam: {total}")

    def _handle_key(self, key: int):
        """Klavye tuş kontrolü."""
        if key == ord("q") or key == 27:  # q veya ESC
            self._running = False

        elif key == ord("r"):  # Reset
            self._reset_counter()

        elif key == ord("d"):  # Debug toggle
            self._debug_mode = not self._debug_mode
            print(f"[PIPELINE] Debug modu: {'AÇIK' if self._debug_mode else 'KAPALI'}")

        elif key == ord("+") or key == ord("="):  # Çizgi yukarı
            pos = self._counting_line.cfg.line_position - 0.02
            self._counting_line.update_line_position(pos)
            print(f"[PIPELINE] Çizgi pozisyonu: {self._counting_line.line_y}")

        elif key == ord("-"):  # Çizgi aşağı
            pos = self._counting_line.cfg.line_position + 0.02
            self._counting_line.update_line_position(pos)
            print(f"[PIPELINE] Çizgi pozisyonu: {self._counting_line.line_y}")

        elif key == ord("s"):  # Screenshot
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = f"screenshot_{ts}.png"
            # Son frame'i kaydet (bir sonraki döngüde)
            print(f"[PIPELINE] Ekran görüntüsü: {path}")

        elif key == ord("f"):  # Fullscreen toggle
            prop = cv2.getWindowProperty(
                self.cfg.pipeline.window_name, cv2.WND_PROP_FULLSCREEN
            )
            if prop == cv2.WINDOW_FULLSCREEN:
                cv2.setWindowProperty(
                    self.cfg.pipeline.window_name,
                    cv2.WND_PROP_FULLSCREEN,
                    cv2.WINDOW_NORMAL
                )
            else:
                cv2.setWindowProperty(
                    self.cfg.pipeline.window_name,
                    cv2.WND_PROP_FULLSCREEN,
                    cv2.WINDOW_FULLSCREEN
                )

        elif key == ord(" "):  # Pause/Resume
            self._paused = not self._paused
            state = "DURAKLATILDI" if self._paused else "DEVAM EDİYOR"
            print(f"[PIPELINE] {state}")

    def _reset_counter(self):
        """Sayacı sıfırla (tracking verilerini de)."""
        if self._counting_line:
            self._counting_line.reset()
        if self._track_manager:
            self._track_manager.reset()
        if self._logger:
            self._logger.reset_counter()
        if self._preprocessor:
            self._preprocessor.reset()
        print("[PIPELINE] Sayaç ve takip verileri sıfırlandı!")

    def _cleanup(self):
        """Kaynakları temizle."""
        print("\n[PIPELINE] Kapatılıyor...")

        if self._logger:
            self._logger.close()

        if self._writer is not None:
            self._writer.release()

        if self._cap is not None:
            self._cap.release()

        cv2.destroyAllWindows()

        total = self._counting_line.total_count if self._counting_line else 0
        print(f"[PIPELINE] Toplam sayım: {total}")
        print("[PIPELINE] Kapatıldı.")

    def get_status(self) -> dict:
        """Sistem durumunu döndür."""
        return {
            "running": self._running,
            "paused": self._paused,
            "debug": self._debug_mode,
            "fps": round(self._fps, 1),
            "total_count": self._counting_line.total_count if self._counting_line else 0,
            "active_tracks": self._track_manager.get_active_count() if self._track_manager else 0,
            "frame": self._track_manager.frame_count if self._track_manager else 0,
            "resolution": f"{self._frame_width}x{self._frame_height}",
        }
