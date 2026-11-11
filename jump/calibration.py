"""Measure landing error by tracking the destination platform across camera motion."""
from __future__ import annotations

from dataclasses import dataclass
import time

import cv2
import numpy as np
import pyautogui

from . import vision


@dataclass
class BoardTracker:
    template: np.ndarray
    mask: np.ndarray
    center_offset: tuple[int, int]

    @classmethod
    def create(cls, frame, result, cfg):
        geo = vision.analyze_board(
            frame, (result.piece_x, result.piece_y),
            cfg['under_game_score_ratio'], cfg['bg_diff_threshold'])
        if geo is None:
            return None
        left, right = geo['left'][0], geo['right'][0]
        width = right - left
        cx, cy = geo['center']
        x0, x1 = max(0, left - 5), min(frame.shape[1], right + 6)
        y0 = max(0, geo['vertex'][1] - 4)
        depth = width * 0.58
        if width < frame.shape[1] * 0.18:
            # A piece can cover nearly the entire top of a small platform.
            # Retain the visible body/label below it as the stable anchor.
            depth = max(depth, frame.shape[0] * 0.16)
        y1 = min(frame.shape[0], cy + int(depth))
        patch = frame[y0:y1, x0:x1].copy()
        if patch.size == 0 or width < 30:
            return None
        bg = vision.background_model(frame)[y0:y1, None, :]
        mask = (np.abs(patch.astype(np.float32) - bg).sum(axis=2) > 40)
        # The piece and its shadow can cover the middle after landing.
        xs = np.arange(x0, x1)[None, :]
        mask &= np.abs(xs - cx) > width * 0.27
        if mask.sum() < 150:
            return None
        return cls(patch, mask.astype(np.uint8) * 255, (cx-x0, cy-y0))

    def locate(self, frame, near_piece=None):
        y0 = int(frame.shape[0] * 0.24)
        search = frame[y0:]
        if search.shape[0] < self.template.shape[0]:
            return None
        scores = cv2.matchTemplate(search, self.template,
                                  cv2.TM_SQDIFF_NORMED, mask=self.mask)
        scores[~np.isfinite(scores)] = 1e6
        if near_piece is not None:
            # Similar white platforms can match better than the real landing
            # platform after the background palette changes. The platform
            # supporting the landed piece must be close to that piece.
            ys, xs = np.ogrid[:scores.shape[0], :scores.shape[1]]
            radius = max(65.0, frame.shape[1] * 0.12)
            dx = xs + self.center_offset[0] - near_piece[0]
            dy = ys + self.center_offset[1] + y0 - near_piece[1]
            scores[dx*dx + dy*dy > radius*radius] = 1e6
        loss, _, location, _ = cv2.minMaxLoc(scores)
        if loss > 0.16:
            return None
        return (location[0] + self.center_offset[0],
                location[1] + self.center_offset[1] + y0, float(loss))


def wait_for_landing(capturer, region, cfg, released_at):
    """Wait for a settled scene; never infer success solely from a timer."""
    previous = None
    stable_since = None
    deadline = released_at + 5.0
    while time.monotonic() < deadline:
        pyautogui.failSafeCheck()
        frame = capturer.grab_bgr(region)
        small = cv2.resize(frame[int(frame.shape[0]*0.22):int(frame.shape[0]*0.92)],
                           (180, 240))
        now = time.monotonic()
        if previous is not None and now - released_at >= 0.55:
            change = np.max(np.abs(small.astype(np.int16)-previous), axis=2)
            still = float(np.mean(change > 12)) < 0.002
            if still:
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= 0.30:
                    piece = vision.find_piece(frame, cfg['piece_color'],
                                              cfg['scan_start_ratio'])
                    if piece is not None:
                        return frame, piece, stable_since
            else:
                stable_since = None
        previous = small.astype(np.int16)
        time.sleep(0.10)
    return None


def signed_error(result, landed_piece, landed_center):
    delta = np.array([result.board_x-result.piece_x,
                      result.board_y-result.piece_y], dtype=float)
    direction = delta / max(np.linalg.norm(delta), 1.0)
    error = np.array(landed_piece, dtype=float)-np.array(landed_center, dtype=float)
    return float(error @ direction), float(np.linalg.norm(error))
