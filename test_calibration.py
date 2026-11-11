"""Synthetic tracking regressions and desktop-free timing checks."""
import json
import math
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

import cv2
import numpy as np

from jump import calibration, control, vision

BASE = Path(__file__).resolve().parent


class CalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = json.loads((BASE/'config.json').read_text(encoding='utf-8'))

    def test_similar_platform_elsewhere_cannot_win_matching(self):
        template = np.full((80, 100, 3), 245, dtype=np.uint8)
        cv2.rectangle(template, (0, 35), (99, 79), (110, 120, 130), -1)
        mask = np.full(template.shape[:2], 255, dtype=np.uint8)
        mask[:, 25:75] = 0
        tracker = calibration.BoardTracker(template, mask, (50, 20))
        frame = np.full((1085, 608, 3), 210, dtype=np.uint8)
        frame[582:662, 182:282] = template - 2
        frame[949:1029, 118:218] = template
        center = tracker.locate(frame, near_piece=(232, 602))
        self.assertIsNotNone(center)
        self.assertLess(math.dist(center[:2], (232, 602)), 2)

    def test_fixed_latency_is_not_scaled_by_distance(self):
        model = dict(self.cfg, press_coefficient=2.0, press_offset_ms=40.0)
        self.assertEqual(control.predict_press_duration(150, model), 340.0)
        self.assertEqual(control.predict_press_duration(300, model), 640.0)
        legacy = dict(model, press_offset_ms=0.0)
        self.assertEqual(control.predict_press_duration(150, legacy), 300.0)

    def test_small_cap_remains_trackable_when_piece_covers_top(self):
        def bottle(frame, x, y):
            cv2.rectangle(frame, (x-32, y+12), (x+32, y+95), (165, 105, 55), -1)
            cv2.ellipse(frame, (x, y+95), (32, 12), 0, 0, 360, (165, 105, 55), -1)
            cv2.ellipse(frame, (x, y), (33, 19), 0, 0, 360, (250, 250, 250), -1)
            cv2.putText(frame, 'TEST', (x-26, y+75), cv2.FONT_HERSHEY_SIMPLEX,
                        0.4, (245, 245, 245), 1)
        before = np.full((1085, 608, 3), (215, 225, 235), dtype=np.uint8)
        bottle(before, 468, 468)
        after = np.full_like(before, (215, 225, 235))
        # An ambiguous top-only patch fits near the piece, but has no body.
        top_only = before[444:505, 431:507]
        after[566:627, 138:214] = top_only
        bottle(after, 241, 599)
        cv2.ellipse(after, (241, 567), (21, 40), 0, 0, 360, (90, 55, 60), -1)
        geometry = dict(center=(468, 468), vertex=(469, 448),
                        left=(436, 468), right=(501, 468), plateau=True)
        result = vision.DetectResult(164, 654, 468, 468)
        piece = (244, 607)
        with patch.object(vision, 'analyze_board', return_value=geometry):
            tracker = calibration.BoardTracker.create(before, result, self.cfg)
        center = tracker.locate(after, near_piece=piece)
        self.assertIsNotNone(center)
        self.assertLess(math.dist(center[:2], (241, 599)), 6)
        self.assertLess(math.dist(piece, center[:2]), 15)

    def test_interval_and_step_limit_preserved(self):
        self.assertEqual(self.cfg['max_score_stop'], 300)
        self.assertEqual(self.cfg['landing_to_takeoff_seconds'], [3.5, 5.0])
        intervals = [control.sample_landing_interval(self.cfg) for _ in range(100)]
        self.assertTrue(all(3.5 <= x <= 5.0 for x in intervals))
        self.assertGreater(len(set(intervals)), 90)

    def test_invalid_press_values_rejected(self):
        for distance in (0, -1, float('nan'), float('inf'), 100000):
            with self.assertRaises(ValueError):
                control.predict_press_duration(distance, self.cfg)

    def test_mouse_released_when_charging_is_interrupted(self):
        mouse = Mock(FAILSAFE=True)
        with patch.object(control, 'pyautogui', mouse), patch.object(
                control, '_precise_sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                control.press_and_hold(100, 200, 500)
        mouse.mouseUp.assert_called_once_with(_pause=False)
        self.assertTrue(mouse.FAILSAFE)

    def test_timed_press_releases_at_deadline(self):
        now = [10.0]
        def advance(seconds):
            now[0] += seconds
        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=advance)
        events = []
        mouse = Mock(FAILSAFE=True)
        mouse.mouseDown.side_effect = lambda **kw: events.append(('down', now[0]))
        mouse.mouseUp.side_effect = lambda **kw: events.append(('up', now[0]))
        with patch.object(control, 'pyautogui', mouse), patch.object(
                control, 'time', clock), patch.object(control, '_precise_sleep', advance):
            actual = control.press_and_hold(100, 200, 500, release_at=12.0)
        self.assertAlmostEqual(events[0][1], 11.5, places=5)
        self.assertAlmostEqual(events[1][1], 12.0, places=5)
        self.assertAlmostEqual(actual, 500.0, places=5)

    def test_missed_deadline_does_not_click(self):
        mouse = Mock(FAILSAFE=True)
        with patch.object(control, 'pyautogui', mouse), patch.object(
                control.time, 'monotonic', return_value=10.0):
            with self.assertRaises(RuntimeError):
                control.press_and_hold(100, 200, 500, release_at=10.2)
        mouse.mouseDown.assert_not_called()


if __name__ == '__main__':
    unittest.main()
