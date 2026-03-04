import unittest

from .tracker import TrackManager
from .config import TrackerConfig, CounterConfig


class TrackerDedupTest(unittest.TestCase):
    def setUp(self):
        self.tracker = TrackManager(TrackerConfig(), CounterConfig())
        # assume 480px high frame with line at middle
        self.tracker.set_frame_height(480)
        self.tracker.set_line_y(240)
        # old counted track at lower half
        self.tracker._lost_track_positions = {100: (200, 300)}
        self.tracker.counted_ids.add(100)
        self.tracker._lost_track_frame[100] = 10
        self.tracker._frame_count = 12

    def test_no_dedup_above_line(self):
        # new detection above line should not be deduped
        self.assertIsNone(self.tracker._check_spatial_dedup(200, 200, 100))

    def test_dedup_below_line(self):
        # new detection well within radius and below line -> should match
        self.assertEqual(self.tracker._check_spatial_dedup(201, 200, 320), 100)


if __name__ == "__main__":
    unittest.main()
