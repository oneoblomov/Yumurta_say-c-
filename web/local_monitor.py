"""
local_monitor.py - Hafif OpenCV yerel izleme penceresi
=======================================================
Web arayuzundeki kamera akisini acmadan, sadece ihtiyac duyuldugunda
acilan dusuk maliyetli bir cv2.imshow penceresi saglar.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

import cv2
import numpy as np


class LocalMonitorWindow:
    """Arka planda calisan hafif yerel monitor penceresi."""

    def __init__(self, pipeline: Any,
                 window_name: str = "Yumurta Sayici - Yerel Monitor",
                 refresh_interval: float = 0.08,
                 today_sync_interval: float = 3.0):
        self.pipeline = pipeline
        self.window_name = window_name
        self.refresh_interval = max(0.03, float(refresh_interval))
        self.today_sync_interval = max(0.5, float(today_sync_interval))

        self._available = bool(
            os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
            or os.name == "nt"
        )
        self._requested_enabled = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._last_error: Optional[str] = None
        self._window_open = False
        self._last_today_sync = 0.0
        self._today_count = 0
        self._last_snapshot_total: Optional[int] = None
        self._font = cv2.FONT_HERSHEY_SIMPLEX

    # ------------------------------------------------------------------ state
    @property
    def available(self) -> bool:
        return self._available

    @property
    def enabled(self) -> bool:
        return self._requested_enabled

    @property
    def is_running(self) -> bool:
        return self._window_open

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def status(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "enabled": self.enabled,
            "running": self.is_running,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ control
    def start(self) -> Dict[str, Any]:
        should_wait_for_open = False
        with self._state_lock:
            if not self.available:
                self._requested_enabled = False
                self._last_error = (
                    "Grafik oturumu gerekli (DISPLAY / WAYLAND)."
                )
                return {"ok": False, "error": self._last_error, **self.status()}

            if self._thread is not None and self._thread.is_alive():
                self._requested_enabled = True
                self._last_error = None
                should_wait_for_open = True
            else:
                self._requested_enabled = True
                self._stop_event.clear()
                self._last_error = None
                self._last_today_sync = 0.0
                try:
                    cv2.startWindowThread()
                except Exception:
                    pass

                self._thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name="LocalMonitorWindow",
                )
                self._thread.start()
                should_wait_for_open = True

        if should_wait_for_open:
            deadline = time.time() + 1.0
            while time.time() < deadline and not self._window_open and not self._stop_event.is_set():
                time.sleep(0.05)

        return {"ok": True, **self.status()}

    def stop(self) -> Dict[str, Any]:
        with self._state_lock:
            self._requested_enabled = False
            self._last_error = None

        return {"ok": True, **self.status()}

    def shutdown(self) -> Dict[str, Any]:
        with self._state_lock:
            self._requested_enabled = False
            self._stop_event.set()
            thread = self._thread

        if thread and thread.is_alive():
            thread.join(timeout=2.5)

        with self._state_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
            self._window_open = False

        return {"ok": True, **self.status()}

    def toggle(self) -> Dict[str, Any]:
        if self.is_running:
            return self.stop()
        return self.start()

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        try:
            try:
                cv2.startWindowThread()
            except Exception:
                pass

            while not self._stop_event.is_set():
                if not self._requested_enabled:
                    if self._window_open:
                        try:
                            cv2.destroyWindow(self.window_name)
                        except Exception:
                            pass
                        self._window_open = False
                    if self._stop_event.wait(self.refresh_interval):
                        break
                    continue

                if not self._window_open:
                    try:
                        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
                        try:
                            cv2.resizeWindow(self.window_name, 960, 720)
                        except Exception:
                            pass
                        self._window_open = True
                        self._last_error = None
                    except Exception as exc:
                        self._last_error = str(exc)
                        self._requested_enabled = False
                        if self._stop_event.wait(0.5):
                            break
                        continue

                frame = self.pipeline.get_latest_display_frame()
                if frame is None:
                    frame = self._build_placeholder_frame()

                display = self._compose_frame(frame)

                try:
                    cv2.imshow(self.window_name, display)
                except Exception as exc:
                    self._last_error = str(exc)
                    self._requested_enabled = False
                    self._window_open = False
                    try:
                        cv2.destroyWindow(self.window_name)
                    except Exception:
                        pass
                    if self._stop_event.wait(0.5):
                        break
                    continue

                try:
                    key = cv2.waitKey(1) & 0xFF
                except Exception:
                    key = -1

                if key in (27, ord("q")):
                    self._requested_enabled = False
                    if self._window_open:
                        try:
                            cv2.destroyWindow(self.window_name)
                        except Exception:
                            pass
                    self._window_open = False
                    continue

                if self._is_window_closed():
                    self._requested_enabled = False
                    self._window_open = False
                    if self._stop_event.wait(self.refresh_interval):
                        break
                    continue

                if self._stop_event.wait(self.refresh_interval):
                    break

        except Exception as exc:
            self._last_error = str(exc)
            self._requested_enabled = False
            self._stop_event.set()
        finally:
            if self._window_open:
                try:
                    cv2.destroyWindow(self.window_name)
                except Exception:
                    pass
            with self._state_lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                self._window_open = False

    # ------------------------------------------------------------------ render
    def _get_today_count(self, current_total: int) -> int:
        now = time.time()
        if (
            self._last_snapshot_total != current_total
            or now - self._last_today_sync >= self.today_sync_interval
        ):
            try:
                self._today_count = int(self.pipeline.db.get_today_count())
                self._last_today_sync = now
                self._last_snapshot_total = current_total
            except Exception:
                pass
        return self._today_count

    def _compose_frame(self, frame: np.ndarray) -> np.ndarray:
        display = frame
        h, w = display.shape[:2]
        snapshot = self.pipeline.get_monitor_snapshot()
        today_count = self._get_today_count(int(snapshot.get("total_count", 0) or 0))

        top_h = min(34, h)
        top = display[:top_h, :]
        top_overlay = top.copy()
        top_overlay[:] = (12, 16, 22)
        cv2.addWeighted(top_overlay, 0.72, top, 0.28, 0, top)

        bottom_h = min(72, h)
        bottom_y = max(0, h - bottom_h)
        bottom = display[bottom_y:, :]
        bottom_overlay = bottom.copy()
        bottom_overlay[:] = (12, 12, 14)
        cv2.addWeighted(bottom_overlay, 0.78, bottom, 0.22, 0, bottom)

        status_text, status_color = self._status_label(snapshot)
        cv2.circle(display, (18, 18), 6, status_color, -1, cv2.LINE_AA)
        cv2.putText(
            display,
            "YEREL IZLEME",
            (34, 23),
            self._font,
            0.55,
            (245, 245, 245),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            display,
            status_text,
            (34, 39 if h > 60 else 30),
            self._font,
            0.45,
            status_color,
            1,
            cv2.LINE_AA,
        )

        fps_text = f"FPS {snapshot['fps']:.1f}"
        fps_x = max(16, w - 110)
        cv2.putText(
            display,
            fps_text,
            (fps_x, 23),
            self._font,
            0.55,
            (220, 220, 220),
            1,
            cv2.LINE_AA,
        )

        y1 = bottom_y + 26
        y2 = min(h - 12, bottom_y + 52)
        cv2.putText(
            display,
            f"BUGUN {today_count}",
            (16, y1),
            self._font,
            0.72,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            display,
            f"OTURUM {snapshot['total_count']}   TAKIP {snapshot['active_tracks']}",
            (16, y2),
            self._font,
            0.55,
            (210, 210, 210),
            1,
            cv2.LINE_AA,
        )

        meta_text = snapshot.get("resolution", "N/A")
        if snapshot.get("camera_open"):
            meta_text = f"{meta_text}   KAMERA ACIK"
        if snapshot.get("last_camera_error"):
            meta_text = f"ERR: {self._truncate(snapshot['last_camera_error'], 34)}"

        cv2.putText(
            display,
            meta_text,
            (max(16, w - 260), y2),
            self._font,
            0.5,
            (160, 160, 160),
            1,
            cv2.LINE_AA,
        )

        return display

    def _status_label(self, snapshot: Dict[str, Any]) -> tuple[str, tuple[int, int, int]]:
        if snapshot.get("running"):
            if snapshot.get("paused"):
                return "DURAKLATILDI", (255, 191, 71)
            return "CALISIYOR", (76, 222, 128)
        return "DURDU", (148, 163, 184)

    def _build_placeholder_frame(self) -> np.ndarray:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:] = (18, 18, 22)
        cv2.putText(
            frame,
            "KAMERA KAPALI",
            (150, 220),
            self._font,
            1.15,
            (200, 200, 200),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            "YEREL MONITORU SOL ALT BUTONDAN ACIN",
            (56, 270),
            self._font,
            0.58,
            (150, 150, 150),
            1,
            cv2.LINE_AA,
        )
        return frame

    def _is_window_closed(self) -> bool:
        try:
            return cv2.getWindowProperty(self.window_name, cv2.WND_PROP_VISIBLE) < 1
        except Exception:
            return True

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        text = str(text)
        if len(text) <= max_len:
            return text
        return text[: max(0, max_len - 3)] + "..."
