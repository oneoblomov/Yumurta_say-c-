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
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Callable

# FFmpeg patch
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "threads;1|fflags;nobuffer")
os.environ.setdefault("OPENCV_FFMPEG_MULTITHREADED", "0")

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from egg_counter.config import SystemConfig
from egg_counter.preprocessor import FramePreprocessor
from egg_counter.detector import EggDetector
from egg_counter.tracker import TrackManager
from egg_counter.counter import CountingLine
from egg_counter.visualizer import Visualizer


class PipelineManager:
    """
    Web-uyumlu pipeline yöneticisi.
    Arka plan thread'inde çalışır, MJPEG streaming ve
    real-time durum güncellemeleri sağlar.
    """

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

        # Stream quality
        self._jpeg_quality = 70

        # No-camera placeholder
        self._placeholder_frame = self._create_placeholder()

    # ------------------------------------------------------------------ public
    def start(self, source: str = None, **overrides) -> Dict:
        """Pipeline başlat."""
        if self._running:
            return {"ok": False, "error": "Pipeline zaten çalışıyor"}

        try:
            config = self._build_config(source, **overrides)
            config.pipeline.headless = True
            self._config = config

            if not self._init_capture():
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
            self._consecutive_failures = 0

            self._thread = threading.Thread(
                target=self._processing_loop, daemon=True
            )
            self._thread.start()

            self._emit_event("pipeline_started", {
                "session_id": self._session_id,
                "source": config.pipeline.source,
            })

            return {"ok": True, "session_id": self._session_id}

        except Exception as e:
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
        """Sayaç sıfırla (pipeline çalışırken)."""
        if self._counting_line:
            self._counting_line.reset()
        if self._track_manager:
            self._track_manager.reset()
        if self._detector:
            self._detector.reset_tracker()
        self._total_count = 0
        self._emit_event("count_reset", {})
        return {"ok": True}

    def toggle_debug(self) -> bool:
        self._debug_mode = not self._debug_mode
        return self._debug_mode

    def get_status(self) -> Dict:
        """Güncel pipeline durumu."""
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
        }
        return status

    def get_frame_jpeg(self) -> Optional[bytes]:
        """Son frame'i JPEG olarak döndür."""
        with self._frame_lock:
            frame = self._frame_buffer
        if frame is None:
            frame = self._placeholder_frame
        if frame is None:
            return None
        _, jpeg = cv2.imencode(
            ".jpg", frame,
            [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality]
        )
        return jpeg.tobytes()

    def frame_generator(self):
        """MJPEG streaming generator."""
        while True:
            self._frame_event.wait(timeout=0.1)
            self._frame_event.clear()
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

        # Counter
        config.counter.post_cross_drop_frames = int(s.get("post_cross_drop", "0"))

        # Preprocessor
        config.preprocessor.enable_clahe = s.get("enable_clahe", "1") == "1"
        config.preprocessor.enable_stabilization = s.get("enable_stabilization", "0") == "1"

        # Pipeline
        config.pipeline.crop_ud = int(s.get("crop_ud", "0"))
        config.pipeline.crop_lr = int(s.get("crop_lr", "0"))

        # Visualizer
        config.visualizer.headless = True  # Web arayüzü için HUD gizle

        # Stream quality
        self._jpeg_quality = int(s.get("stream_quality", "70"))

        # Apply overrides
        for k, v in kw.items():
            if hasattr(config.pipeline, k):
                setattr(config.pipeline, k, v)
            elif hasattr(config.detector, k):
                setattr(config.detector, k, v)

        return config

    def _init_capture(self) -> bool:
        """Kamera/video kaynağını başlat."""
        source = self._config.pipeline.source

        src_str = str(source)
        is_video_file = src_str != "" and not src_str.isdigit()

        try:
            if isinstance(source, str) and source.isdigit():
                source = int(source)

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

            ret, frame = self._capture.read()
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
                    self._emit_alert(
                        "camera_disconnect",
                        "Kamera bağlantısı kesildi!",
                        "critical",
                    )
                    self._running = False
                    if self._session_id:
                        self.db.update_session_status(
                            self._session_id, "error")
                    break

                time.sleep(0.01)
                continue

            self._consecutive_failures = 0
            self._frame_count += 1

            t0 = time.perf_counter()
            display = self._process_frame(frame)
            t1 = time.perf_counter()

            # FPS
            self._fps_times.append(t1 - t0)
            if len(self._fps_times) >= 5:
                avg = sum(self._fps_times) / len(self._fps_times)
                self._fps = 1.0 / max(avg, 1e-6)

            # Buffer frame
            with self._frame_lock:
                self._frame_buffer = display
            self._frame_event.set()

        # Loop ended
        with self._frame_lock:
            self._frame_buffer = None
        self._frame_event.set()

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        """Tek frame pipeline (pipeline.py ile aynı mantık)."""
        # Crop
        if self._crop_top or self._crop_left:
            frame = frame[self._crop_top:self._crop_bottom,
                          self._crop_left:self._crop_right]

        # Preprocess
        processed = self._preprocessor.process(frame)

        # YOLO + Track (ROI)
        roi_top = self._counting_line.roi_top_y
        roi_bot = self._counting_line.roi_bottom_y
        roi_frame = processed[roi_top:roi_bot, :]
        result = self._detector.detect_and_track(roi_frame)
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

        # Update DB session count periodically
        if self._session_id and self._frame_count % 30 == 0:
            try:
                self.db.update_session_count(
                    self._session_id, self._total_count)
            except Exception:
                pass

        # Trails
        trails = {}
        for det in enriched:
            tid = det.get("track_id")
            if tid is not None:
                trails[tid] = self._track_manager.get_trail(tid)

        # Visualize
        display = self._visualizer.draw(
            frame=processed,
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

        # Goal check
        self._check_goals(event.get("total", 0))

        print(f"[WEB SAYIM] #{event.get('track_id', '?')} "
              f"-> Toplam: {event.get('total', 0)}")

    def _check_goals(self, total: int):
        """Hedef kontrolü."""
        try:
            goals = self.db.get_active_goals()
            for g in goals:
                if g["type"] == "daily":
                    today_count = self.db.get_today_count()
                    if today_count >= g["target_count"]:
                        self._emit_event("goal_reached", {
                            "type": "daily",
                            "target": g["target_count"],
                            "actual": today_count,
                        })
                        self.db.add_alert(
                            "goal_reached",
                            f"Günlük hedef tamamlandı! "
                            f"{today_count}/{g['target_count']}",
                            "info",
                        )
        except Exception:
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
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._preprocessor = None
        self._detector = None
        self._track_manager = None
        self._counting_line = None
        self._visualizer = None

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
