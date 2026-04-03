import unittest
from datetime import datetime

from .pipeline_manager import PipelineManager


class DummyDB:
    def __init__(self):
        self.updated = False
        self.last_args = None
        self.session_created = False
        self.settings = {
            "camera_active_start": "08:00",
            "camera_active_end": "16:00",
        }

    def create_session(self, *args, **kwargs):
        self.session_created = True
        return 1
    def end_session(self, *args, **kwargs):
        pass
    def update_session_count(self, session_id, count):
        self.updated = True
        self.last_args = (session_id, count)
    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


class DummyTrackManager:
    def __init__(self):
        self.reset_called = False
    def reset(self):
        self.reset_called = True


class DummyDetector:
    def __init__(self):
        self.reset_called = False
    def reset_tracker(self):
        self.reset_called = True


class DummyCountingLine:
    def __init__(self):
        self.reset_called = False
        self.total_count = 5
    def reset(self):
        self.reset_called = True
        self.total_count = 0


class DummyLocalMonitor:
    def __init__(self):
        self.enabled = False
        self.is_running = False
        self.available = True
        self.last_error = None

    def toggle(self):
        self.enabled = not self.enabled
        self.is_running = self.enabled
        return {
            "ok": True,
            "enabled": self.enabled,
            "running": self.is_running,
            "available": self.available,
            "last_error": self.last_error,
        }


class PipelineManagerResetTest(unittest.TestCase):
    def setUp(self):
        self.db = DummyDB()
        self.pm = PipelineManager(db=self.db)
        # simulate an active session so DB updates are triggered
        self.pm._session_id = 42

        # attach simple stub modules
        self.pm._counting_line = DummyCountingLine()
        self.pm._track_manager = DummyTrackManager()
        self.pm._detector = DummyDetector()
        self.pm._local_monitor = DummyLocalMonitor()
        self.pm._total_count = 5

    def test_reset_only_count(self):
        # invoking reset_count should zero only the visible counter and
        # call reset on counting_line, leaving tracker/detector untouched.
        self.pm.reset_count()
        self.assertEqual(self.pm._total_count, 0)
        self.assertTrue(self.pm._counting_line.reset_called)
        self.assertFalse(self.pm._track_manager.reset_called,
                         "track manager should not be reset on web reset")
        self.assertFalse(self.pm._detector.reset_called,
                         "detector tracker should not be reset on web reset")
        # DB should have been updated to zero
        self.assertTrue(self.db.updated)
        self.assertEqual(self.db.last_args, (42, 0))

class PipelineManagerScheduleTest(unittest.TestCase):
    def setUp(self):
        self.db = DummyDB()
        self.pm = PipelineManager(db=self.db)

    def test_schedule_defaults_are_exposed(self):
        self.assertEqual(
            self.pm.get_schedule_window(),
            {"start": "08:00", "end": "16:00"},
        )

    def test_schedule_window_check(self):
        self.assertFalse(
            self.pm.is_within_schedule(
                now=datetime.strptime("07:59", "%H:%M").time()
            )
        )
        self.assertTrue(
            self.pm.is_within_schedule(
                now=datetime.strptime("08:00", "%H:%M").time()
            )
        )
        self.assertTrue(
            self.pm.is_within_schedule(
                now=datetime.strptime("15:59", "%H:%M").time()
            )
        )
        self.assertFalse(
            self.pm.is_within_schedule(
                now=datetime.strptime("16:00", "%H:%M").time()
            )
        )

    def test_start_blocked_outside_schedule(self):
        self.pm.is_within_schedule = lambda now=None: False
        result = self.pm.start(source="0")
        self.assertFalse(result["ok"])
        self.assertIn("08:00-16:00", result["error"])
        self.assertFalse(self.db.session_created)

    def test_local_monitor_state_is_exposed(self):
        status = self.pm.get_status()
        self.assertIn("local_monitor_enabled", status)
        self.assertFalse(status["local_monitor_enabled"])

        result = self.pm.toggle_local_monitor()
        self.assertTrue(result["ok"])
        self.assertTrue(self.pm.get_status()["local_monitor_enabled"])
        self.assertTrue(self.pm.get_status()["local_monitor_running"])


if __name__ == "__main__":
    unittest.main()
