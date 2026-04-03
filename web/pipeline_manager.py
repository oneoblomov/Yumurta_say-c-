"""
pipeline_manager.py - Web Pipeline Yöneticisi
===============================================
Mevcut EggCountingPipeline'ı web arayüzü için sarar.
Arka plan thread'inde çalışır, frame buffer ve olay sistemi sağlar.
"""

import os
import sys
import time
import json
import threading
import queue
import numpy as np
from collections import deque
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional, Dict, List, Callable

# FFmpeg patch
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "threads;1|fflags;nobuffer")
os.environ.setdefault("OPENCV_FFMPEG_MULTITHREADED", "0")

import cv2

# RPi5: 4x ARM Cortex-A76 çekirdek için OpenCV thread sayısı optimizasyonu
try:
    cv2.setNumThreads(4)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from egg_counter.config import SystemConfig
from egg_counter.preprocessor import FramePreprocessor
from egg_counter.detector import EggDetector
from egg_counter.tracker import TrackManager
from egg_counter.counter import CountingLine
from egg_counter.visualizer import Visualizer
from .local_monitor import LocalMonitorWindow


class AsyncDatasetRecorder:
    """Write frames to disk in a background thread to avoid blocking inference."""

    def __init__(self, output_dir: Path,
                 jpeg_quality: int = 95,
                 max_queue: int = 16):
        self.output_dir = Path(output_dir)
        self.jpeg_quality = max(50, min(int(jpeg_quality), 100))
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def enqueue(self, frame: np.ndarray, total_count: int) -> bool:
        if not self._running:
            return False

        ts = datetime.now()
        day_dir = self.output_dir / ts.strftime("%Y-%m-%d")
        filename = f"egg_{ts.strftime('%H%M%S_%f')}_total_{int(total_count):06d}.jpg"
        item = (frame.copy(), day_dir / filename)

        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            # Drop oldest frame to protect the main pipeline under load.
            try:
                _ = self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
                return True
            except queue.Full:
                return False

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break

            frame, path = item
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(
                    str(path),
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality],
                )
            except Exception:
                # Recorder failures must not affect counting.
                pass


class PipelineManager:
    """
    Web-uyumlu pipeline yöneticisi.
    Arka plan thread'inde çalışır, MJPEG streaming ve
    real-time durum güncellemeleri sağlar.
    """

    DEFAULT_CAMERA_ACTIVE_START = "08:00"
    DEFAULT_CAMERA_ACTIVE_END = "16:00"

    def __init__(self, db):
        self.db = db

        # Config & modules
        self._config: Optional[SystemConfig] = None
        self._capture = None
        self._preprocessor: Optional[FramePreprocessor] = None
        self._detector: Optional[EggDetector] = None
        self._track_manager: Optional[TrackManager] = None
        self._counting_line: Optional[CountingLine] = None
        self._visualizer: Optional[Visualizer] = None

        # Frame buffer (thread-safe)
        self._frame_buffer: Optional[np.ndarray] = None
        self._frame_jpeg_buffer: Optional[bytes] = None
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Event()
        self._frame_width = 0
        self._frame_height = 0

        # Crop
        self._crop_top = 0
        self._crop_bottom = 0
        self._crop_left = 0
        self._crop_right = 0

        # State
        self._running = False
        self._paused = False
        self._debug_mode = False
        self._thread: Optional[threading.Thread] = None
        self._session_id: Optional[int] = None

        # FPS
        self._fps = 0.0
        self._fps_times: deque = deque(maxlen=30)
        self._last_session_sync_at = 0.0

        # Counters
        self._total_count = 0
        self._active_tracks = 0
        self._frame_count = 0

        # Event queue (for WebSocket/SSE broadcast)
        self._event_queue: queue.Queue = queue.Queue(maxsize=1000)
        self._recent_events: deque = deque(maxlen=50)

        # Alert tracking
        self._consecutive_failures = 0
        self._last_alert_time = 0.0
        self._last_camera_error: Optional[str] = None

        # Stream quality
        self._jpeg_quality = 50  # Optimized for bandwidth (was 70)

        # Dataset capture for YOLO re-training
        self._dataset_capture_enabled = False
        self._dataset_capture_interval_sec = 5.0
        self._last_dataset_capture_at = 0.0
        self._dataset_recorder: Optional[AsyncDatasetRecorder] = None

        # No-camera placeholder
        self._placeholder_frame = self._create_placeholder()
        self._placeholder_jpeg = self._encode_jpeg(self._placeholder_frame)

        # Local OpenCV monitor (toggled from the web UI)
        self._local_monitor = LocalMonitorWindow(self)

    # ------------------------------------------------------------------ public
    def start(self, source: str = None, **overrides) -> Dict:
        """Pipeline başlat."""
        if self._running:
            return {"ok": False, "error": "Pipeline zaten çalışıyor"}

        if not self.is_within_schedule():
            window = self.get_schedule_window()
            return {
                "ok": False,
                "error": (
                    "Kamera yalnızca planlanan saatlerde çalışır "
                    f"({window['start']}-{window['end']})"
                ),
            }

        try:
            config = self._build_config(source, **overrides)
            config.pipeline.headless = True
            self._config = config

            if not self._init_capture():
                self._last_camera_error = "Kamera açılamadı"
                self._emit_alert("camera_start_failed", self._last_camera_error,
                                 "error")
                return {"ok": False, "error": "Kamera açılamadı"}

            self._init_modules()

            # DB session
            self._session_id = self.db.create_session(
                source=config.pipeline.source,
                config_json=json.dumps(config.to_dict(), default=str),
            )

            self._running = True
            self._paused = False
            self._total_count = 0
            self._frame_count = 0
            self._fps = 0.0
            self._fps_times.clear()
            self._last_session_sync_at = 0.0
            self._consecutive_failures = 0
            self._last_camera_error = None
            self._last_dataset_capture_at = 0.0

            if self._dataset_capture_enabled:
                self._dataset_recorder = AsyncDatasetRecorder(
                    output_dir=ROOT / "dataset" / "raw" / "training_capture",
                    jpeg_quality=95,
                    max_queue=16,
                )
                self._dataset_recorder.start()

            self._thread = threading.Thread(
                target=self._processing_loop, daemon=True
            )
            self._thread.start()

            # Kamera bekçisi thread'ini başlat
            self._watchdog_thread = threading.Thread(
                target=self._camera_watchdog, daemon=True
            )
            self._watchdog_thread.start()

            self._emit_event("pipeline_started", {
                "session_id": self._session_id,
                "source": config.pipeline.source,
            })

            return {"ok": True, "session_id": self._session_id}

        except Exception as e:
            self._last_camera_error = str(e)
            self._emit_alert("error", f"Pipeline başlatma hatası: {e}",
                             "error")
            return {"ok": False, "error": str(e)}

    def stop(self) -> Dict:
        """Pipeline durdur."""
        if not self._running:
            return {"ok": False, "error": "Pipeline çalışmıyor"}

        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if hasattr(self, '_watchdog_thread') and self._watchdog_thread:
            self._watchdog_thread.join(timeout=5)
        self._cleanup()

        # DB session kapat
        if self._session_id:
            self.db.end_session(self._session_id, self._total_count)
            self._emit_event("pipeline_stopped", {
                "session_id": self._session_id,
                "total_count": self._total_count,
            })
            self._session_id = None

        return {"ok": True, "total_count": self._total_count}

    def pause(self) -> Dict:
        if not self._running:
            return {"ok": False, "error": "Pipeline çalışmıyor"}
        self._paused = True
        if self._session_id:
            self.db.update_session_status(self._session_id, "paused")
        self._emit_event("pipeline_paused", {})
        return {"ok": True}

    def resume(self) -> Dict:
        if not self._running:
            return {"ok": False, "error": "Pipeline çalışmıyor"}
        self._paused = False
        if self._session_id:
            self.db.update_session_status(self._session_id, "running")
        self._emit_event("pipeline_resumed", {})
        return {"ok": True}

    def reset_count(self) -> Dict:
        """Sadece sayacı sıfırla.

        Web arayüzündeki "Sıfırla" butonu yalnızca görünen sayımı 0'a çevirmeli;
        kamera pipeline'ın diğer durumunu bozmamalıdır. Bu, CLI uygulamasındaki
        `r` tuşuna bastığımızda olan tüm modüllerin sıfırlanmasından farklıdır.
        Bu yöntem yalnızca iç sayacı ve veriyi 0'a çekip bir olay yayınlar.
        """
        # counting_line.reset() sıfır sayıyı tutar
        if self._counting_line:
            self._counting_line.reset()

        # pipeline toplamını sıfırla (görünen değer)
        self._total_count = 0

        # DB oturumu varsa hemen güncelle, böylece arayüz sorgulasa 0 döner
        if self._session_id:
            try:
                self.db.update_session_count(self._session_id, 0)
            except Exception:
                pass

        # olay yayınla (UI hemen güncellesin)
        self._emit_event("count_reset", {})
        return {"ok": True}

    def toggle_debug(self) -> bool:
        self._debug_mode = not self._debug_mode
        return self._debug_mode

    def get_status(self) -> Dict:
        """Güncel pipeline durumu."""
        schedule = self.get_schedule_window()
        monitor = getattr(self, "_local_monitor", None)
        status = {
            "running": self._running,
            "paused": self._paused,
            "debug": self._debug_mode,
            "fps": round(self._fps, 1),
            "total_count": self._total_count,
            "active_tracks": self._active_tracks,
            "frame_count": self._frame_count,
            "session_id": self._session_id,
            "resolution": (f"{self._frame_width}x{self._frame_height}"
                           if self._frame_width else "N/A"),
            "schedule_start": schedule["start"],
            "schedule_end": schedule["end"],
            "schedule_active": self.is_within_schedule(),
            "camera_open": self.is_open(),
            "last_camera_error": self._last_camera_error,
            "local_monitor_enabled": bool(getattr(monitor, "enabled", False)),
            "local_monitor_running": bool(getattr(monitor, "is_running", False)),
            "local_monitor_available": bool(getattr(monitor, "available", False)),
            "local_monitor_error": getattr(monitor, "last_error", None),
        }
        return status

    def get_latest_display_frame(self) -> Optional[np.ndarray]:
        """En son islenmis frame'in kopyasini dondur."""
        with self._frame_lock:
            frame = self._frame_buffer
            if frame is None:
                return None
            return frame.copy()

    def get_monitor_snapshot(self) -> Dict[str, object]:
        """Yerel monitor overlaysi icin hafif durum ozeti."""
        return {
            "running": self._running,
            "paused": self._paused,
            "fps": round(self._fps, 1),
            "total_count": self._total_count,
            "active_tracks": self._active_tracks,
            "frame_count": self._frame_count,
            "resolution": (
                f"{self._frame_width}x{self._frame_height}"
                if self._frame_width else "N/A"
            ),
            "camera_open": self.is_open(),
            "last_camera_error": self._last_camera_error,
        }

    def start_local_monitor(self) -> Dict:
        return self._local_monitor.start()

    def stop_local_monitor(self) -> Dict:
        return self._local_monitor.stop()

    def shutdown_local_monitor(self) -> Dict:
        return self._local_monitor.shutdown()

    def toggle_local_monitor(self) -> Dict:
        return self._local_monitor.toggle()

    def get_local_monitor_status(self) -> Dict[str, object]:
        return self._local_monitor.status()

    def get_frame_jpeg(self) -> Optional[bytes]:
        """Son frame'i JPEG olarak döndür."""
        with self._frame_lock:
            jpeg = self._frame_jpeg_buffer
        if jpeg is not None:
            return jpeg
        return self._placeholder_jpeg

    def frame_generator(self):
        """
        MJPEG streaming generator with FPS throttling.
        Limits streaming FPS to stream_fps_limit setting (default 10 FPS).
        
        This significantly reduces bandwidth when streaming over cloudflared:
        - 10 FPS limit = up to 3x bandwidth reduction
        - Combined with JPEG quality reduction (70→50) and compression
          optimizations = 5-6x total bandwidth reduction
        """
        fps_limit = int(self.db.get_setting("stream_fps_limit", "10"))
        fps_limit = max(1, min(fps_limit, 30))  # Clamp: 1-30 FPS
        frame_interval = 1.0 / fps_limit  # seconds between frames
        last_frame_time = 0.0
        
        while True:
            current_time = time.perf_counter()
            time_since_last = current_time - last_frame_time
            
            # Wait if not enough time has passed for next frame
            if time_since_last < frame_interval:
                wait_time = frame_interval - time_since_last
                self._frame_event.wait(timeout=wait_time)
                self._frame_event.clear()
                continue
            
            last_frame_time = current_time
            jpeg = self.get_frame_jpeg()
            if jpeg:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n"
                       + jpeg + b"\r\n")
            
            if not self._running:
                # Bağlantı hâlâ açıksa placeholder gönder
                jpeg = self.get_frame_jpeg()
                if jpeg:
                    yield (b"--frame\r\n"
                           b"Content-Type: image/jpeg\r\n\r\n"
                           + jpeg + b"\r\n")
                time.sleep(0.5)

    def get_new_events(self, max_count: int = 20) -> List[Dict]:
        """Kuyruktan yeni olayları al."""
        events = []
        while len(events) < max_count:
            try:
                ev = self._event_queue.get_nowait()
                events.append(ev)
            except queue.Empty:
                break
        return events

    def get_recent_events(self) -> List[Dict]:
        return list(self._recent_events)

    # ------------------------------------------------------------------ private
    def _build_config(self, source: str = None, **kw) -> SystemConfig:
        """DB ayarlarından + override'lardan config oluştur."""
        s = self.db.get_settings()
        config = SystemConfig()

        # Camera
        config.pipeline.source = source or s.get("camera_source", "0")
        config.pipeline.camera_width = int(s.get("camera_width", "640"))
        config.pipeline.camera_height = int(s.get("camera_height", "480"))

        # Detector
        config.detector.model_path = s.get(
            "model_path", "models/yolo26n_mod/best_openvino_model")
        config.detector.conf_threshold = float(
            s.get("conf_threshold", "0.30"))
        config.detector.iou_threshold = float(
            s.get("iou_threshold", "0.45"))
        config.detector.imgsz = int(s.get("imgsz", "480"))

        # Counter
        config.counter.line_position = float(
            s.get("line_position", "0.5"))
        config.counter.direction = s.get("direction", "top_to_bottom")
        config.counter.roi_top_position = float(
            s.get("roi_top", "0.25"))
        config.counter.roi_bottom_position = float(
            s.get("roi_bottom", "0.75"))

        # Tracker
        config.tracker.tracker_type = s.get("tracker_type", "bytetrack")
        config.tracker.track_buffer = int(s.get("track_buffer", "90"))
        config.tracker.match_thresh = float(s.get("match_thresh", "0.85"))

        # Dense mod için özel YOLO NMS eşiği (bitişik yumurtalar daha iyi ayrılsın)
        if config.tracker.tracker_type == "dense":
            config.detector.iou_threshold = min(
                config.detector.iou_threshold, 0.30)
            config.detector.conf_threshold = min(
                config.detector.conf_threshold, 0.25)
            # CLAHE zaten açık olmalı; clip limitini yükselterek kenar kontrastı artır
            config.preprocessor.enable_clahe = True

        # Counter
        config.counter.post_cross_drop_frames = int(s.get("post_cross_drop", "0"))

        # Preprocessor - "1"/"0" veya "true"/"false" formatını kabul et
        def _as_bool(val: str, default: str = "0") -> bool:
            v = str(s.get(val, default)).strip().lower()
            return v in ("1", "true", "yes")

        config.preprocessor.enable_clahe = _as_bool("enable_clahe", "1")
        config.preprocessor.enable_stabilization = _as_bool("enable_stabilization", "0")

        # Pipeline
        config.pipeline.crop_ud = int(s.get("crop_ud", "0"))
        config.pipeline.crop_lr = int(s.get("crop_lr", "0"))

        # Visualizer
        config.visualizer.headless = True  # Web arayüzü için HUD gizle

        # Stream quality
        self._jpeg_quality = int(s.get("stream_quality", "70"))
        self._placeholder_jpeg = self._encode_jpeg(self._placeholder_frame)

        # Dataset capture toggle (UI settings)
        v = str(s.get("dataset_capture_enabled", "0")).strip().lower()
        self._dataset_capture_enabled = v in ("1", "true", "yes")

        # Apply overrides
        for k, v in kw.items():
            if hasattr(config.pipeline, k):
                setattr(config.pipeline, k, v)
            elif hasattr(config.detector, k):
                setattr(config.detector, k, v)

        return config

    def _init_capture(self) -> bool:
        """Kamera/video kaynağını başlat (RPi5: V4L2 backend öncelikli)."""
        source = self._config.pipeline.source

        src_str = str(source)
        is_video_file = src_str != "" and not src_str.isdigit()

        try:
            if isinstance(source, str) and source.isdigit():
                source = int(source)

            # --- V4L2 backend: RPi5'te latency ve CPU açısından daha verimli ---
            cap = None
            _backend = cv2.CAP_ANY
            backend_cfg = self._config.pipeline.camera_backend
            if not is_video_file and isinstance(source, int) and backend_cfg in ("auto", "v4l2"):
                try:
                    cap_try = cv2.VideoCapture(source, cv2.CAP_V4L2)
                    if cap_try.isOpened():
                        cap = cap_try
                        _backend = cv2.CAP_V4L2
                        print("[WEB CAPTURE] Backend: V4L2 (RPi5 optimal)")
                    else:
                        cap_try.release()
                        print("[WEB CAPTURE] V4L2 açılamadı, AUTO")
                except Exception:
                    pass

            if cap is None:
                cap = cv2.VideoCapture(source)

            if not cap.isOpened():
                raise RuntimeError(f"Video kaynağı açılamadı: {source}")

            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if isinstance(source, int):
                cap.set(cv2.CAP_PROP_FRAME_WIDTH,
                        self._config.pipeline.camera_width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT,
                        self._config.pipeline.camera_height)
                cap.set(cv2.CAP_PROP_FPS,
                        self._config.pipeline.camera_fps)
                # MJPEG: USB kameralarda düşük bant genişliğinde daha yüksek FPS
                if _backend == cv2.CAP_V4L2:
                    cap.set(cv2.CAP_PROP_FOURCC,
                            cv2.VideoWriter_fourcc(*"MJPG"))

            self._capture = cap
            self._frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self._frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self._is_video_file = is_video_file

            # Crop
            cfg = self._config.pipeline
            ud = max(0, min(cfg.crop_ud, 90))
            lr = max(0, min(cfg.crop_lr, 90))
            self._crop_top = int(self._frame_height * (ud / 2) / 100)
            self._crop_bottom = (self._frame_height
                                 - int(self._frame_height * (ud / 2) / 100))
            self._crop_left = int(self._frame_width * (lr / 2) / 100)
            self._crop_right = (self._frame_width
                                - int(self._frame_width * (lr / 2) / 100))

            if ud > 0 or lr > 0:
                self._frame_height = self._crop_bottom - self._crop_top
                self._frame_width = self._crop_right - self._crop_left

            return True

        except Exception as e:
            print(f"[WEB PIPELINE] Kamera hatası: {e}")
            return False

    def is_open(self) -> bool:
        """Kamera açık mı kontrol et."""
        return self._capture is not None and self._capture.isOpened()

    @classmethod
    def normalize_schedule_value(cls, value: str, default: str) -> str:
        raw = str(value or default).strip()
        try:
            parsed = datetime.strptime(raw, "%H:%M").time()
        except ValueError:
            parsed = datetime.strptime(default, "%H:%M").time()
        return parsed.strftime("%H:%M")

    def get_schedule_window(self) -> Dict[str, str]:
        start = self.normalize_schedule_value(
            self.db.get_setting(
                "camera_active_start", self.DEFAULT_CAMERA_ACTIVE_START
            ),
            self.DEFAULT_CAMERA_ACTIVE_START,
        )
        end = self.normalize_schedule_value(
            self.db.get_setting(
                "camera_active_end", self.DEFAULT_CAMERA_ACTIVE_END
            ),
            self.DEFAULT_CAMERA_ACTIVE_END,
        )
        return {"start": start, "end": end}

    def is_within_schedule(self, now: Optional[dt_time] = None) -> bool:
        current = now or datetime.now().time().replace(second=0, microsecond=0)
        schedule = self.get_schedule_window()
        start = datetime.strptime(schedule["start"], "%H:%M").time()
        end = datetime.strptime(schedule["end"], "%H:%M").time()

        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def reopen(self):
        """Kamerayı yeniden aç."""
        if self._capture is not None:
            self._capture.release()
        self._init_capture()

    def _recover_camera(self) -> bool:
        """Kamera bağlantısını tekrar kurmayı dene."""
        self._last_camera_error = "Kamera bağlantısı kesildi, yeniden bağlanılıyor"
        self._emit_alert("camera_disconnect", self._last_camera_error,
                         "critical")
        try:
            self.reopen()
        except Exception as exc:
            self._last_camera_error = f"Kamera yeniden açılamadı: {exc}"
            self._emit_alert("camera_reopen_failed", self._last_camera_error,
                             "error")
            return False

        if self.is_open():
            self._consecutive_failures = 0
            self._last_camera_error = None
            self._emit_event("camera_recovered", {
                "message": "Kamera bağlantısı geri geldi",
            })
            return True

        self._last_camera_error = "Kamera yeniden açılamadı"
        self._emit_alert("camera_reopen_failed", self._last_camera_error,
                         "error")
        return False

    def _camera_watchdog(self):
        """Aktif çalışma saatlerinde her 5 saniyede bir kamerayı kontrol et."""
        while self._running:
            if self.is_within_schedule():
                if not self.is_open():
                    print("[WATCHDOG] Kamera kapalı, yeniden açılıyor")
                    try:
                        self.reopen()
                    except Exception as e:
                        print(f"[WATCHDOG] Kamerayı açamadık: {e}")
                        self._last_camera_error = str(e)
            time.sleep(5)

    def _init_modules(self):
        """Alt modülleri başlat."""
        self._preprocessor = FramePreprocessor(self._config.preprocessor)
        self._detector = EggDetector(
            self._config.detector, self._config.tracker)
        self._track_manager = TrackManager(
            self._config.tracker,
            self._config.counter,
            trail_length=self._config.pipeline.trail_length,
        )
        self._counting_line = CountingLine(
            self._config.counter, self._frame_height)
        self._visualizer = Visualizer(
            self._config.visualizer, self._config.counter)
        self._counting_line.on_count(self._on_egg_counted)

    def _processing_loop(self):
        """Ana işleme döngüsü (arka plan thread)."""
        while self._running:
            if self._paused:
                time.sleep(0.05)
                continue

            capture = self._capture
            if capture is None:
                ret, frame = False, None
            else:
                try:
                    ret, frame = capture.read()
                except Exception:
                    ret, frame = False, None

            if not ret or frame is None:
                self._consecutive_failures += 1

                if self._is_video_file:
                    self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    if self._track_manager:
                        self._track_manager.reset()
                    if self._detector:
                        self._detector.reset_tracker()
                    self._consecutive_failures = 0
                    continue

                if self._consecutive_failures > 30:
                    recovered = self.is_within_schedule() and self._recover_camera()
                    if recovered:
                        continue

                    if self._session_id:
                        self.db.update_session_status(
                            self._session_id, "error")
                    time.sleep(1.0)
                    continue

                time.sleep(0.01)
                continue

            self._consecutive_failures = 0
            self._last_camera_error = None
            self._frame_count += 1

            t0 = time.perf_counter()
            display = self._process_frame(frame)
            t1 = time.perf_counter()

            # FPS
            self._fps_times.append(t1 - t0)
            if len(self._fps_times) >= 5:
                avg = sum(self._fps_times) / len(self._fps_times)
                self._fps = 1.0 / max(avg, 1e-6)

            jpeg = self._encode_jpeg(display)

            # Buffer frame
            with self._frame_lock:
                self._frame_buffer = display
                self._frame_jpeg_buffer = jpeg
            self._frame_event.set()

        # Loop ended
        with self._frame_lock:
            self._frame_buffer = None
            self._frame_jpeg_buffer = None
        self._frame_event.set()

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        """
        Tek frame pipeline (pipeline.py ile aynı mantık).

        RPi5 OPTİMİZASYON: CLAHE sadece ROI bandına uygulanır.
        Tam frame'e sadece parlaklık normalizasyonu (hafif); CLAHE ~3-4x daha az piksel işler.
        """
        # Crop
        if self._crop_top or self._crop_left:
            frame = frame[self._crop_top:self._crop_bottom,
                          self._crop_left:self._crop_right]

        # Hafif ön işleme (parlaklık normalizasyonu) – tam frame, CLAHE'siz
        display_frame = self._preprocessor.process_light(frame)

        # ROI dilimi – YOLO sadece bu bant
        roi_top = self._counting_line.roi_top_y
        roi_bot = self._counting_line.roi_bottom_y
        roi_raw = display_frame[roi_top:roi_bot, :]       # view (kopya yok)

        # CLAHE sadece küçük ROI slice üzerinde (in-place → display_frame güncellenir)
        roi_processed = self._preprocessor.process(roi_raw)

        # YOLO + Track (ROI)
        result = self._detector.detect_and_track(roi_processed)
        detections = self._detector.parse_results(result)

        # ROI offset
        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            det["bbox"] = (x1, y1 + roi_top, x2, y2 + roi_top)
            cx, cy = det["center"]
            det["center"] = (cx, cy + roi_top)

        # Track
        enriched = self._track_manager.update(detections)
        self._active_tracks = self._track_manager.get_active_count()

        # Counting line
        newly_counted = self._counting_line.check_crossings(
            enriched, self._track_manager)
        self._total_count = self._counting_line.total_count

        # Save a raw, annotation-free frame at most once every 5s,
        # but only after a new egg has been counted.
        now = time.time()
        if (
            newly_counted
            and
            self._dataset_capture_enabled
            and self._dataset_recorder is not None
            and (now - self._last_dataset_capture_at) >= self._dataset_capture_interval_sec
        ):
            self._dataset_recorder.enqueue(frame, self._total_count)
            self._last_dataset_capture_at = now

        # Update DB session count periodically
        if self._session_id and now - self._last_session_sync_at >= 2.0:
            try:
                self.db.update_session_count(
                    self._session_id, self._total_count)
                self._last_session_sync_at = now
            except Exception:
                pass

        # Trails
        trails = {}
        for det in enriched:
            tid = det.get("track_id")
            if tid is not None:
                trails[tid] = self._track_manager.get_trail(tid)

        # Visualize (display_frame: brightness-normalized + ROI CLAHE'li)
        display = self._visualizer.draw(
            frame=display_frame,
            detections=enriched,
            counting_line_y=self._counting_line.line_y,
            roi_top_y=roi_top,
            roi_bottom_y=roi_bot,
            total_count=self._total_count,
            active_tracks=self._active_tracks,
            fps=self._fps,
            frame_width=self._frame_width,
            trails=trails,
            debug_mode=self._debug_mode,
            show_trails=True,
            newly_counted=newly_counted,
        )

        if self._debug_mode:
            self._visualizer.draw_debug_info(display, {
                "Frame": self._frame_count,
                "Det": len(detections),
                "Tracked": sum(1 for d in enriched if d.get("track_id")),
                "Counted": len(self._track_manager.counted_ids),
                "LineY": self._counting_line.line_y,
                "ROI": f"{roi_top}-{roi_bot}",
            })

        return display

    def _on_egg_counted(self, event: Dict):
        """Sayım olayı callback."""
        # DB'ye kaydet
        if self._session_id:
            try:
                self.db.add_count_event(self._session_id, event)
            except Exception as e:
                print(f"[WEB PIPELINE] DB kayıt hatası: {e}")

        # Event queue
        ev = {
            "type": "egg_counted",
            "track_id": event.get("track_id"),
            "total": event.get("total", 0),
            "confidence": event.get("confidence", 0),
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }
        self._recent_events.appendleft(ev)
        try:
            self._event_queue.put_nowait(ev)
        except queue.Full:
            pass


    def _emit_event(self, event_type: str, data: Dict):
        ev = {"type": event_type, **data,
              "timestamp": datetime.now().strftime("%H:%M:%S")}
        self._recent_events.appendleft(ev)
        try:
            self._event_queue.put_nowait(ev)
        except queue.Full:
            pass

    def _emit_alert(self, alert_type: str, message: str,
                    severity: str = "warning"):
        now = time.time()
        if now - self._last_alert_time < 5:
            return
        self._last_alert_time = now

        self.db.add_alert(alert_type, message, severity)
        self._emit_event("alert", {
            "alert_type": alert_type,
            "message": message,
            "severity": severity,
        })

    def _cleanup(self):
        if self._dataset_recorder is not None:
            self._dataset_recorder.stop()
            self._dataset_recorder = None
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        with self._frame_lock:
            self._frame_buffer = None
            self._frame_jpeg_buffer = None
        self._preprocessor = None
        self._detector = None
        self._track_manager = None
        self._counting_line = None
        self._visualizer = None

    def _encode_jpeg(self, frame: np.ndarray) -> Optional[bytes]:
        """
        JPEG encoding with compression optimizations:
        - Chroma Subsampling 4:2:0 (IMWRITE_JPEG_OPTIMIZE)
        - Progressive JPEG (IMWRITE_JPEG_PROGRESSIVE)
        - Reduces file size by 40-50% compared to baseline
        """
        ok, jpeg = cv2.imencode(
            ".jpg", frame,
            [
                cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality,
                cv2.IMWRITE_JPEG_OPTIMIZE, 1,           # Enable chroma subsampling 4:2:0
                cv2.IMWRITE_JPEG_PROGRESSIVE, 1,        # Enable progressive JPEG
            ]
        )
        if not ok:
            return None
        return jpeg.tobytes()

    def _create_placeholder(self) -> np.ndarray:
        """Kamera kapalıyken gösterilecek placeholder frame."""
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:] = (30, 30, 30)
        cv2.putText(
            frame, "KAMERA KAPALI",
            (160, 230), cv2.FONT_HERSHEY_SIMPLEX,
            1.2, (100, 100, 100), 2, cv2.LINE_AA,
        )
        cv2.putText(
            frame, "Baslatmak icin BASLAT butonuna basin",
            (90, 280), cv2.FONT_HERSHEY_SIMPLEX,
            0.6, (80, 80, 80), 1, cv2.LINE_AA,
        )
        return frame

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused
