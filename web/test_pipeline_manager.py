import unittest

from .pipeline_manager import PipelineManager


class DummyDB:
    def __init__(self):
        self.updated = False
        self.last_args = None

    def create_session(self, *args, **kwargs):
        return 1
    def end_session(self, *args, **kwargs):
        pass
    def update_session_count(self, session_id, count):
        self.updated = True
        self.last_args = (session_id, count)
    def get_setting(self, key, default=None):
        settings = {
            "test_mode_enabled": "1",
            "test_expected_batch": "30",
            "test_window_seconds": "5",
        }
        return settings.get(key, default)


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


class PipelineManagerTestModeStatsTest(unittest.TestCase):
    def setUp(self):
        self.db = DummyDB()
        self.pm = PipelineManager(db=self.db)
        self.pm._test_mode_enabled = True
        self.pm._test_expected_per_series = 30
        self.pm._test_series_timeout_seconds = 5.0
        self.pm._total_count = 0
        self.pm._reset_test_metrics()

    def test_test_mode_batches_and_summary(self):
        base = 1000.0
        self.pm._test_started_at = base

        # Seri-1: 30 yumurta, sonra 5+ sn sessizlikte kapanır -> [30/30]
        for i in range(30):
            self.pm._on_test_egg_counted({"timestamp": base + (i * 0.02)})
        self.pm._close_timed_out_series(now=base + 5.6)

        # Seri-2: 29 yumurta, sonra kapanır -> [29/30]
        s2 = base + 10.0
        for i in range(29):
            self.pm._on_test_egg_counted({"timestamp": s2 + (i * 0.02)})
        self.pm._close_timed_out_series(now=s2 + 5.6)

        # Seri-3: 31 yumurta, sonra kapanır -> [31/30]
        s3 = base + 20.0
        for i in range(31):
            self.pm._on_test_egg_counted({"timestamp": s3 + (i * 0.02)})
        self.pm._close_timed_out_series(now=s3 + 5.7)

        status = self.pm.get_test_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["summary"]["batch_count"], 3)
        self.assertEqual(status["summary"]["actual_total"], 90)
        self.assertEqual(status["summary"]["expected_total"], 90)
        self.assertEqual(status["summary"]["error_total"], 0)
        self.assertFalse(status["active_series"]["active"])

        labels = [b["label"] for b in status["batches"]]
        self.assertEqual(labels, ["[30/30]", "[29/30]", "[31/30]"])


if __name__ == "__main__":
    unittest.main()
