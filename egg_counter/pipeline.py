"""
pipeline.py - Ana Orkestratör (RPi5 Optimize)
================================================
Düzeltmeler:
  1. Threaded camera capture: Ayrı thread frame okur, ana thread beklemez.
     RPi5'te ~5ms/frame tasarruf (kamera I/O bekleme süresi sıfır).
  2. Koordinat tutarlılığı: Stabilizasyon frame geometrisini değiştiriyorsa,
     aynı frame hem algılama hem görselleştirme için kullanılıyor.
     ESKİ HATA: Algılama preprocessed frame'e, görselleştirme orijinal frame'e
     yapılıyordu -> koordinat uyumsuzluğu.
  3. Video loop'ta tracker state sıfırlanıyor (eski: stale ID'ler kalıyordu).
  4. Screenshot tuşu gerçekten kaydediyor (eski: sadece print).
  5. Headless mode: Ekransız RPi5 çalışması.
  6. FPS hesaplama: Sadece inference süresini ölçer (vizualizasyon hariç).
"""

import os
import cv2
import time
import threading
import numpy as np
from collections import deque
from typing import Optional

# FFmpeg tek thread (libavcodec/pthread_frame.c assert crash önlemek için)
# main.py orada zaten ayarlar, ama pipeline doğrudan import edilirse diye burada da.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "threads;1|fflags;nobuffer")
os.environ.setdefault("OPENCV_FFMPEG_MULTITHREADED", "0")

# ─── RPi5 OpenCV İş Parçacığı Optimizasyonu ─────────────────────────────────
# Raspberry Pi 5: 4x ARM Cortex-A76 çekirdek.
# OpenCV varsayılan olarak fazla thread spawn eder → önbelleksiz küçük operasyonlar
# için context-switch maliyeti kazançtan ağır basar.
# 4 thread: CLAHE, warpAffine, resize gibi OpenCV parallel operasyonlarını
# doğal olarak 4 çekirdeğe dağıtır.
try:
    cv2.setNumThreads(4)
except Exception:
    pass
# ─────────────────────────────────────────────────────────────────────────────

from .config import SystemConfig
from .preprocessor import FramePreprocessor
from .detector import EggDetector
from .tracker import TrackManager
from .counter import CountingLine
from .visualizer import Visualizer
from .logger import CountLogger


class ThreadedCapture:
    """
    Ayrı thread'de kamera okuma.
    ana thread cv2.read() I/O beklemesi yapmaz -> ~5ms/frame tasarruf.

    RPi5 OPTİMIZASYON:
    - V4L2 backend tercih edilir: Linux kernel doğrudan MMAP buffer erişimi,
      GStreamer/FFmpeg decode overhead yok -> latency düşer, CPU kullanımı azalır.
    - MJPEG: USB kameralardan daha yüksek FPS kapasitesi.
    - buffer_size=1: hep en son frame (stale frame birikmesi yok).
    """

    def __init__(self, source, width: int, height: int, fps: int, buffer_size: int,
                 backend: str = "auto"):
        self._source = source
        self._width = width
        self._height = height
        self._fps = fps
        self._buffer_size = buffer_size
        self._backend = backend

        if isinstance(source, str) and source.isdigit():
            source = int(source)

        # --- Backend seçimi (RPi5 için V4L2 öncelikli) ---
        _backend = cv2.CAP_ANY
        if backend in ("v4l2", "auto") and isinstance(source, int):
            try:
                cap_try = cv2.VideoCapture(source, cv2.CAP_V4L2)
                if cap_try.isOpened():
                    self._cap = cap_try
                    _backend = cv2.CAP_V4L2
                    print("[CAPTURE] Backend: V4L2 (RPi5 optimal)")
                else:
                    cap_try.release()
                    self._cap = cv2.VideoCapture(source)
                    print("[CAPTURE] Backend: AUTO (V4L2 açılamadı)")
            except Exception:
                self._cap = cv2.VideoCapture(source)
                print("[CAPTURE] Backend: AUTO (V4L2 desteklenmiyor)")
        else:
            self._cap = cv2.VideoCapture(source)

        if not self._cap.isOpened():
            raise RuntimeError(f"Video kaynağı açılamadı: {source}")

        # buffer_size=1: minimum gecikme, sadece en son frame tutulur
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, buffer_size)
        try:
            self._cap.set(cv2.CAP_PROP_THREAD_COUNT, 1)  # type: ignore[attr-defined]
        except Exception:
            pass

        if isinstance(source, int):
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self._cap.set(cv2.CAP_PROP_FPS, fps)
            # RPi5: MJPEG ile USB kameralardan daha yüksek FPS
            if _backend == cv2.CAP_V4L2:
                self._cap.set(cv2.CAP_PROP_FOURCC,
                              cv2.VideoWriter_fourcc(*"MJPG"))

        self.frame_width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.frame_height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        self._frame = None
        self._ret = False
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._is_file = isinstance(source, str)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        # İlk frame'i bekle
        time.sleep(0.1)
        return self

    def _capture_loop(self):
        while self._running:
            ret, frame = self._cap.read()
            with self._lock:
                self._ret = ret
                self._frame = frame
            if not ret:
                time.sleep(0.01)

    def read(self):
        with self._lock:
            return self._ret, self._frame

    def stop(self):
        self._running = False
        if self._thread is not None:
            # Önce thread'in bitmesini bekle, SONRA cap’ı serbest bırak.
            # FFmpeg decoder thread'i cap.read() içindeyken cap.release() çağrırırsak
            # libavcodec mutex assert crash olur.
            self._thread.join(timeout=3.0)
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def reset_position(self):
        """Video dosyasını başa sar."""
        if self._cap is not None:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    def is_open(self):
        return self._cap is not None and self._cap.isOpened()

    def reopen(self):
        # Kapalıysa yeniden oluşturmak için mevcut parametreleri kullan
        self.stop()
        self.__init__(self._source, self._width, self._height, self._fps, self._buffer_size, self._backend)
        self.start()

    @property
    def is_file(self):
        return self._is_file


class DirectCapture:
    """Thread'siz doğrudan kamera okuma (fallback)."""

    def __init__(self, source, width: int, height: int, fps: int, buffer_size: int):
        if isinstance(source, str) and source.isdigit():
            source = int(source)

        self._cap = cv2.VideoCapture(source)
        if not self._cap.isOpened():
            raise RuntimeError(f"Video kaynağı açılamadı: {source}")

        # FIX: FFmpeg thread'lerini 1'e çek
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, buffer_size)
        try:
            self._cap.set(cv2.CAP_PROP_THREAD_COUNT, 1)  # type: ignore[attr-defined]
        except Exception:
            pass

        if isinstance(source, int):
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self._cap.set(cv2.CAP_PROP_FPS, fps)

        self.frame_width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.frame_height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self._is_file = isinstance(source, str)

    def start(self):
        return self

    def read(self):
        return self._cap.read()

    def stop(self):
        if self._cap is not None:
            self._cap.release()

    def reset_position(self):
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    @property
    def is_file(self):
        return self._is_file


class EggCountingPipeline:
    """
    Endüstriyel gerçek zamanlı yumurta sayma pipeline'ı.
    RPi5 optimize: threaded capture, zero-copy viz, lightweight preprocess.
    """

    def __init__(self, config: SystemConfig):
        self.cfg = config
        self._running = False
        self._paused = False
        self._debug_mode = config.pipeline.debug_mode

        # FPS: deque tabanlı (daha stabil ölçüm)
        self._fps = 0.0
        self._fps_times = deque(maxlen=30)

        # Modüller
        self._preprocessor: Optional[FramePreprocessor] = None
        self._detector: Optional[EggDetector] = None
        self._track_manager: Optional[TrackManager] = None
        self._counting_line: Optional[CountingLine] = None
        self._visualizer: Optional[Visualizer] = None
        self._logger: Optional[CountLogger] = None

        # Capture
        self._capture = None
        self._frame_width = 0
        self._frame_height = 0

        # Kenar kırpma pikselleri (crop_ud / crop_lr'dan hesaplanır)
        self._crop_top = 0
        self._crop_bottom = 0
        self._crop_left = 0
        self._crop_right = 0

        # Video yazıcı
        self._writer: Optional[cv2.VideoWriter] = None

        # Son frame (screenshot için)
        self._last_display_frame: Optional[np.ndarray] = None

    def _init_capture(self) -> bool:
        """Kamera/video kaynağını başlat."""
        source = self.cfg.pipeline.source

        # Video DOSYASI için ThreadedCapture kullanma:
        # FFmpeg dahilî decoder thread'leri + bizim okuma thread'imiz çakışır
        # -> libavcodec/pthread_frame.c assertion crash.
        # Kamera (int index) için ise thread'leme I/O gecikmesini ortadan kaldırır.
        src_str = str(source)
        is_video_file = src_str != "" and not src_str.isdigit()
        use_threaded = self.cfg.pipeline.use_threaded_capture and not is_video_file

        try:
            if use_threaded:
                self._capture = ThreadedCapture(
                    source,
                    self.cfg.pipeline.camera_width,
                    self.cfg.pipeline.camera_height,
                    self.cfg.pipeline.camera_fps,
                    self.cfg.pipeline.buffer_size,
                    backend=self.cfg.pipeline.camera_backend,
                ).start()
                print("[PIPELINE] Kamera: threaded capture")
            else:
                self._capture = DirectCapture(
                    source,
                    self.cfg.pipeline.camera_width,
                    self.cfg.pipeline.camera_height,
                    self.cfg.pipeline.camera_fps,
                    self.cfg.pipeline.buffer_size,
                ).start()
                if is_video_file:
                    print("[PIPELINE] Video dosyası: direct capture (FFmpeg thread güvenliği)")
        except RuntimeError as e:
            print(f"[PIPELINE] HATA: {e}")
            return False

        self._frame_width = self._capture.frame_width
        self._frame_height = self._capture.frame_height
        print(f"[PIPELINE] Kaynak açıldı: {self._frame_width}x{self._frame_height}")

        # Kenar kırpma piksel hesabı
        # crop_ud / 2  üstten, crop_ud / 2 alttan (yüzde)
        # crop_lr / 2  soldan, crop_lr / 2 sağdan (yüzde)
        ud = max(0, min(self.cfg.pipeline.crop_ud, 90))   # güvenlik: en fazla %90
        lr = max(0, min(self.cfg.pipeline.crop_lr, 90))
        self._crop_top    = int(self._frame_height * (ud / 2) / 100)
        self._crop_bottom = self._frame_height - int(self._frame_height * (ud / 2) / 100)
        self._crop_left   = int(self._frame_width  * (lr / 2) / 100)
        self._crop_right  = self._frame_width  - int(self._frame_width  * (lr / 2) / 100)

        if ud > 0 or lr > 0:
            # Modüller kırpılmış boyutu kullanmalı
            self._frame_height = self._crop_bottom - self._crop_top
            self._frame_width  = self._crop_right  - self._crop_left
            print(f"[PIPELINE] Kırpma: üst-alt=%{ud} sol-sağ=%{lr} "
                  f"-> etkin çözünürlük: {self._frame_width}x{self._frame_height}")

        return True

    def _init_modules(self):
        print("[PIPELINE] Modüller başlatılıyor...")

        self._preprocessor = FramePreprocessor(self.cfg.preprocessor)

        print("[PIPELINE] YOLO model yükleniyor...")
        self._detector = EggDetector(self.cfg.detector, self.cfg.tracker)

        self._track_manager = TrackManager(
            self.cfg.tracker, self.cfg.counter,
            trail_length=self.cfg.pipeline.trail_length
        )
        # inform tracker about frame height now (line_y will follow after counting_line created)
        if self._frame_height:
            self._track_manager.set_frame_height(self._frame_height)

        self._counting_line = CountingLine(self.cfg.counter, self._frame_height)
        # after counting line exists we can send its y-coordinate
        if self._track_manager:
            self._track_manager.set_line_y(self._counting_line.line_y)
        self._visualizer = Visualizer(self.cfg.visualizer, self.cfg.counter)
        self._logger = CountLogger(self.cfg.logger)

        self._counting_line.on_count(self._on_egg_counted)

        print(f"[PIPELINE] Hazır. Çizgi y={self._counting_line.line_y}, "
              f"ROI y={self._counting_line.roi_top_y}-{self._counting_line.roi_bottom_y}, "
              f"Cihaz={self.cfg.detector.device}")

    def _init_video_writer(self):
        if not self.cfg.pipeline.save_output:
            return
        fourcc = cv2.VideoWriter_fourcc(*self.cfg.pipeline.output_codec)
        self._writer = cv2.VideoWriter(
            self.cfg.pipeline.output_path, fourcc,
            self.cfg.pipeline.target_fps,
            (self._frame_width, self._frame_height)
        )

    def run(self):
        """Ana çalışma döngüsü."""
        print("=" * 60)
        print("  YUMURTA SAYICI v2.0 - RPi5 Optimize")
        print("  Azim-Tav Endüstriyel Sayım Sistemi")
        print("=" * 60)

        if not self._init_capture():
            return

        self._init_modules()
        self._init_video_writer()

        # Pencere (headless değilse)
        if not self.cfg.pipeline.headless:
            window = self.cfg.pipeline.window_name
            cv2.namedWindow(window, cv2.WINDOW_NORMAL)
            if self.cfg.pipeline.fullscreen:
                cv2.setWindowProperty(window, cv2.WND_PROP_FULLSCREEN,
                                      cv2.WINDOW_FULLSCREEN)

        self._running = True
        frame_count = 0
        skip = self.cfg.pipeline.skip_frames
        consecutive_failures = 0

        print(f"\n[PIPELINE] Çalışıyor... (q:çıkış r:sıfırla d:debug +/-:çizgi)")

        try:
            while self._running:
                # Tuş kontrolü
                if not self.cfg.pipeline.headless:
                    key = cv2.waitKey(1) & 0xFF
                    self._handle_key(key)

                if self._paused:
                    time.sleep(0.01)
                    continue

                # Frame oku
                ret, frame = self._capture.read()
                if not ret or frame is None:
                    consecutive_failures += 1
                    if self._capture.is_file:
                        # Video bitti -> sıfırla ve tekrar başlat
                        self._capture.reset_position()
                        # DÜZELTME: Tracker'ı sıfırla (eski: stale ID'ler kalıyordu)
                        self._track_manager.reset()
                        self._detector.reset_tracker()
                        consecutive_failures = 0
                        continue
                    elif consecutive_failures > 90:  # ~3 saniye (30 FPS * 3) kısa kesintileri tolere et
                        print("[PIPELINE] Kamera bağlantısı kesildi, yeniden bağlanılıyor…")
                        try:
                            self._capture.reopen()
                            self._track_manager.reset()
                            self._detector.reset_tracker()
                            consecutive_failures = 0
                            time.sleep(0.5)  # Yeniden açılması için zaman ver
                            continue
                        except Exception as exc:
                            print(f"[PIPELINE] Kamera yeniden açılamadı: {exc}")
                            break
                    time.sleep(0.01)
                    continue

                consecutive_failures = 0
                frame_count += 1

                # Frame skip
                if skip > 0 and frame_count % (skip + 1) != 0:
                    continue

                # === ANA PIPELINE ===
                t_start = time.perf_counter()
                display_frame = self._process_frame(frame)
                t_end = time.perf_counter()

                # FPS (sadece inference + tracking, vizualizasyon dahil)
                self._fps_times.append(t_end - t_start)
                if len(self._fps_times) >= 5:
                    avg = sum(self._fps_times) / len(self._fps_times)
                    self._fps = 1.0 / max(avg, 1e-6)

                self._last_display_frame = display_frame

                # Video kayıt
                if self._writer is not None:
                    self._writer.write(display_frame)

                # Ekran
                if not self.cfg.pipeline.headless:
                    cv2.imshow(self.cfg.pipeline.window_name, display_frame)

        except KeyboardInterrupt:
            print("\n[PIPELINE] Kullanıcı durdurdu.")
        finally:
            self._cleanup()

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        """
        Tek frame pipeline.

        RPi5 OPTİMİZASYON - ROI-ÖNCELİKLİ CLANE:
          ESKİ: CLAHE tüm 640x480 frame'e uygulanıyordu (~307K piksel).
          YENİ: Tam frame'e sadece parlaklık normalizasyonu (hızlı),
                CLAHE sadece küçük ROI bant dilimine uygulanır (~92K piksel @%30 bant).
                Bu ~3-4x daha az CLAHE işlemi demektir.

        DÜZELTME: Aynı frame hem algılama hem görselleştirme için kullanılıyor.
        Eski: algılama=preprocessed, görselleştirme=orijinal -> koordinat UYUMSUZLUĞU.
        """
        # 0. Kenar kırpma (FPS artırmak için – küçük frame, hızlı inference)
        if self._crop_top or self._crop_left:
            frame = frame[self._crop_top:self._crop_bottom,
                          self._crop_left:self._crop_right]

        # 1. Hafif ön işleme – tam frame'e sadece parlaklık normalizasyonu
        #    (CLAHE UYGULANMAZ – sadece display için gerekli, inference ROI'de yapılır)
        display_frame = self._preprocessor.process_light(frame)

        # 2. ROI dilimi – YOLO sadece bu bant içini görecek
        roi_top_y = self._counting_line.roi_top_y
        roi_bottom_y = self._counting_line.roi_bottom_y
        roi_raw = display_frame[roi_top_y:roi_bottom_y, :]   # view (kopya değil)

        # 3. CLAHE sadece küçük ROI bant üzerinde (bu bant inference'a gider)
        #    process() in-place değiştirir; roi_raw view olduğu için display_frame
        #    üzerindeki ROI bölgesi de güncellenir (sıfır kopya).
        roi_processed = self._preprocessor.process(roi_raw)

        # 4. YOLO + Tracker – sadece CLAHE'li ROI bant
        result = self._detector.detect_and_track(roi_processed)
        detections = self._detector.parse_results(result)

        # ROI koordinat ofseti: tüm bbox ve center y'lerini tam frame'e çevir
        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            det["bbox"] = (x1, y1 + roi_top_y, x2, y2 + roi_top_y)
            cx, cy = det["center"]
            det["center"] = (cx, cy + roi_top_y)

        # 5. Track yönetimi + spatial dedup
        enriched = self._track_manager.update(detections)

        # 6. Sayım çizgisi kontrolü
        newly_counted = self._counting_line.check_crossings(
            enriched, self._track_manager
        )

        # 7. Trail verisi (görselleştirme)
        trails = {}
        if self.cfg.pipeline.show_track_trail:
            for det in enriched:
                tid = det.get("track_id")
                if tid is not None:
                    trails[tid] = self._track_manager.get_trail(tid)

        # 8. Görselleştir (display_frame: parlaklık normalize + ROI kısmı CLAHE'li)
        display = self._visualizer.draw(
            frame=display_frame,
            detections=enriched,
            counting_line_y=self._counting_line.line_y,
            roi_top_y=roi_top_y,
            roi_bottom_y=roi_bottom_y,
            total_count=self._counting_line.total_count,
            active_tracks=self._track_manager.get_active_count(),
            fps=self._fps,
            frame_width=self._frame_width,
            trails=trails,
            debug_mode=self._debug_mode,
            show_trails=self.cfg.pipeline.show_track_trail,
            newly_counted=newly_counted,
        )

        # 9. Debug
        if self._debug_mode:
            self._visualizer.draw_debug_info(display, {
                "Frame": self._track_manager.frame_count,
                "Det": len(detections),
                "Tracked": sum(1 for d in enriched if d.get("track_id")),
                "Counted": len(self._track_manager.counted_ids),
                "LineY": self._counting_line.line_y,
                "ROI": f"{roi_top_y}-{roi_bottom_y}",
                "Conf": self.cfg.detector.conf_threshold,
                "Lost": len(self._track_manager._lost_track_positions),
            })

        return display

    def _on_egg_counted(self, event: dict):
        if self._logger:
            self._logger.log_count_event(event)
        tid = event.get("track_id", "?")
        total = event.get("total", 0)
        print(f"[SAYIM] #{tid} -> Toplam: {total}")

    def _handle_key(self, key: int):
        if key == ord("q") or key == 27:
            self._running = False

        elif key == ord("r"):
            self._reset_counter()

        elif key == ord("d"):
            self._debug_mode = not self._debug_mode
            print(f"[PIPELINE] Debug: {'AÇIK' if self._debug_mode else 'KAPALI'}")

        elif key == ord("+") or key == ord("="):
            pos = self._counting_line.cfg.line_position - 0.02
            self._counting_line.update_line_position(pos)
            if self._track_manager:
                self._track_manager.set_line_y(self._counting_line.line_y)
            print(f"[PIPELINE] Çizgi: y={self._counting_line.line_y}")

        elif key == ord("-"):
            pos = self._counting_line.cfg.line_position + 0.02
            self._counting_line.update_line_position(pos)
            if self._track_manager:
                self._track_manager.set_line_y(self._counting_line.line_y)
            print(f"[PIPELINE] Çizgi: y={self._counting_line.line_y}")

        elif key == ord("s"):
            # DÜZELTME: Gerçekten kaydediyor (eski: sadece print ediyordu)
            if self._last_display_frame is not None:
                ts = time.strftime("%Y%m%d_%H%M%S")
                path = f"screenshot_{ts}.png"
                cv2.imwrite(path, self._last_display_frame)
                print(f"[PIPELINE] Screenshot: {path}")

        elif key == ord("f"):
            wn = self.cfg.pipeline.window_name
            prop = cv2.getWindowProperty(wn, cv2.WND_PROP_FULLSCREEN)
            new_prop = cv2.WINDOW_NORMAL if prop == cv2.WINDOW_FULLSCREEN else cv2.WINDOW_FULLSCREEN
            cv2.setWindowProperty(wn, cv2.WND_PROP_FULLSCREEN, new_prop)

        elif key == ord(" "):
            self._paused = not self._paused
            print(f"[PIPELINE] {'DURAKLATILDI' if self._paused else 'DEVAM'}")

    def _reset_counter(self):
        if self._counting_line:
            self._counting_line.reset()
        if self._track_manager:
            self._track_manager.reset()
        if self._detector:
            self._detector.reset_tracker()
        if self._logger:
            self._logger.reset_counter()
        if self._preprocessor:
            self._preprocessor.reset()
        print("[PIPELINE] Tümü sıfırlandı!")

    def _cleanup(self):
        print("\n[PIPELINE] Kapatılıyor...")
        if self._logger:
            self._logger.close()
        if self._writer is not None:
            self._writer.release()
        if self._capture is not None:
            self._capture.stop()
        if not self.cfg.pipeline.headless:
            cv2.destroyAllWindows()

        total = self._counting_line.total_count if self._counting_line else 0
        print(f"[PIPELINE] Toplam: {total}")

    def get_status(self) -> dict:
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
