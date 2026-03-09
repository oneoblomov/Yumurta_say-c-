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

        # Test mode metrics
        self._test_lock = threading.Lock()
        self._test_mode_enabled = False
        self._test_expected_per_series = 55
        self._test_series_timeout_seconds = 5.0
        self._test_started_at: Optional[float] = None
        self._test_series_active = False
        self._test_series_start_at: Optional[float] = None
        self._test_last_egg_at: Optional[float] = None
        self._test_series_count = 0
        self._test_series_index = 0
        self._test_batches: deque = deque(maxlen=2000)  # tamamlanmış seriler

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
            self._init_test_mode_from_settings()
            self._reset_test_metrics()

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

    def get_test_status(self) -> Dict:
        """Test modu metriklerini döndür (5 sn sessizlik-seri analizi)."""
        with self._test_lock:
            batches = list(self._test_batches)
            active = {
                "active": self._test_series_active,
                "index": self._test_series_index + (1 if self._test_series_active else 0),
                "started_at": (
                    datetime.fromtimestamp(self._test_series_start_at).strftime("%H:%M:%S")
                    if self._test_series_start_at else None
                ),
                "count": self._test_series_count,
                "last_egg_at": (
                    datetime.fromtimestamp(self._test_last_egg_at).strftime("%H:%M:%S")
                    if self._test_last_egg_at else None
                ),
                "idle_seconds": 0.0,
                "remaining_seconds": self._test_series_timeout_seconds,
                "label": f"[{self._test_series_count}/{self._test_expected_per_series}]",
            }

        total_batches = len(batches)
        actual_total = sum(b["actual"] for b in batches)
        expected_total = sum(b["expected"] for b in batches)

        if active["active"]:
            actual_total += active["count"]
            expected_total += self._test_expected_per_series

        error_total = actual_total - expected_total

        if total_batches > 0 and expected_total > 0:
            mape = (
                sum(abs(b["diff"]) / max(1, b["expected"]) for b in batches)
                / total_batches
            ) * 100.0
        else:
            mape = 0.0

        accuracy = max(0.0, 100.0 - mape)
        last_batch = batches[-1] if batches else None

        now = time.time()
        elapsed = 0.0
        if self._test_started_at:
            elapsed = max(0.0, now - self._test_started_at)

        if active["active"] and self._test_last_egg_at is not None:
            idle = max(0.0, now - self._test_last_egg_at)
            active["idle_seconds"] = round(idle, 2)
            active["remaining_seconds"] = round(
                max(0.0, self._test_series_timeout_seconds - idle), 2
            )

        return {
            "enabled": self._test_mode_enabled,
            "running": self._running,
            "window_seconds": self._test_series_timeout_seconds,
            "expected_per_window": self._test_expected_per_series,
            "elapsed_seconds": round(elapsed, 1),
            "total_count": self._total_count,
            "summary": {
                "batch_count": total_batches,
                "actual_total": actual_total,
                "expected_total": expected_total,
                "error_total": error_total,
                "mape": round(mape, 2),
                "accuracy": round(accuracy, 2),
            },
            "active_series": active,
            "last_batch": last_batch,
            "batches": batches[-40:],
        }

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

    def reopen(self):
        """Kamerayı yeniden aç."""
        if self._capture is not None:
            self._capture.release()
        self._init_capture()

    def _camera_watchdog(self):
        """08-18 arası her 5 saniyede bir kamerayı kontrol et."""
        while self._running:
            now = datetime.now().time()
            # Geçici test: her zaman kontrol et (saat aralığı devre dışı)
            # if datetime.time(hour=8) <= now < datetime.time(hour=18):
            if True:  # Geçici: her zaman aktif
                if not self.is_open():
                    print("[WATCHDOG] Kamera kapalı, yeniden açılıyor")
                    try:
                        self.reopen()
                    except Exception as e:
                        print(f"[WATCHDOG] Kamerayı açamadık: {e}")
                        raise  # Uygulamayı çökert, systemd yeniden başlatır
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

            # Test mode: aktif seri 5 sn sessizlik kontrolü
            self._close_timed_out_series(now=time.time())

            # Buffer frame
            with self._frame_lock:
                self._frame_buffer = display
            self._frame_event.set()

        # Loop ended
        self._close_active_series(reason="pipeline_stopped", now=time.time())
        with self._frame_lock:
            self._frame_buffer = None
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
        self._on_test_egg_counted(event)

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

    def _init_test_mode_from_settings(self):
        """DB ayarlarından test modu parametrelerini yükle."""
        try:
            enabled = str(self.db.get_setting("test_mode_enabled", "1"))
            expected = int(self.db.get_setting("test_expected_batch", "55"))
            window_sec = float(self.db.get_setting("test_window_seconds", "5"))

            self._test_mode_enabled = enabled == "1"
            self._test_expected_per_series = max(1, expected)
            self._test_series_timeout_seconds = max(1.0, window_sec)
        except Exception:
            self._test_mode_enabled = True
            self._test_expected_per_series = 55
            self._test_series_timeout_seconds = 5.0

    def _reset_test_metrics(self):
        with self._test_lock:
            now = time.time()
            self._test_started_at = now
            self._test_series_active = False
            self._test_series_start_at = None
            self._test_last_egg_at = None
            self._test_series_count = 0
            self._test_series_index = 0
            self._test_batches.clear()

    def _event_time(self, event: Dict) -> float:
        ts = event.get("timestamp")
        if isinstance(ts, (int, float)):
            return float(ts)
        return time.time()

    def _on_test_egg_counted(self, event: Dict):
        """Yumurta sayıldığında seri durumunu güncelle."""
        if not self._test_mode_enabled:
            return

        now = self._event_time(event)

        with self._test_lock:
            # Arada 5+ sn sessizlik oluşmuşsa eski seriyi kapat, yenisini başlat.
            if self._test_series_active and self._test_last_egg_at is not None:
                idle = now - self._test_last_egg_at
                if idle >= self._test_series_timeout_seconds:
                    self._close_active_series_locked("idle_timeout", now)

            if not self._test_series_active:
                self._test_series_active = True
                self._test_series_start_at = now
                self._test_last_egg_at = now
                self._test_series_count = 1
                self._emit_event("test_series_started", {
                    "series_index": self._test_series_index + 1,
                    "started_at": datetime.fromtimestamp(now).strftime("%H:%M:%S"),
                })
                return

            self._test_series_count += 1
            self._test_last_egg_at = now

    def _close_timed_out_series(self, now: Optional[float] = None):
        """Aktif seride 5 sn yumurta gelmezse seriyi kapat."""
        if not self._test_mode_enabled:
            return

        if now is None:
            now = time.time()

        with self._test_lock:
            if not self._test_series_active or self._test_last_egg_at is None:
                return
            if now - self._test_last_egg_at >= self._test_series_timeout_seconds:
                self._close_active_series_locked("idle_timeout", now)

    def _close_active_series(self, reason: str, now: Optional[float] = None):
        if not self._test_mode_enabled:
            return
        if now is None:
            now = time.time()
        with self._test_lock:
            self._close_active_series_locked(reason, now)

    def _close_active_series_locked(self, reason: str, now: float):
        if not self._test_series_active or self._test_series_start_at is None:
            return

        self._test_series_index += 1
        actual = int(self._test_series_count)
        expected = int(self._test_expected_per_series)
        diff = actual - expected
        err_pct = (abs(diff) / max(1, expected)) * 100.0
        acc_pct = max(0.0, 100.0 - err_pct)

        batch = {
            "index": self._test_series_index,
            "start": datetime.fromtimestamp(self._test_series_start_at).strftime("%H:%M:%S"),
            "end": datetime.fromtimestamp(now).strftime("%H:%M:%S"),
            "actual": actual,
            "expected": expected,
            "diff": int(diff),
            "error_pct": round(err_pct, 2),
            "accuracy": round(acc_pct, 2),
            "label": f"[{actual}/{expected}]",
            "reason": reason,
        }
        self._test_batches.append(batch)

        self._emit_event("test_series_closed", {
            "series_index": self._test_series_index,
            "result": batch["label"],
            "reason": reason,
        })

        self._test_series_active = False
        self._test_series_start_at = None
        self._test_last_egg_at = None
        self._test_series_count = 0

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
