"""鼠标按压模拟：长按时长决定跳跃距离。

防检测设计：
- 按压点在游戏窗口下半部随机选取（不要每次按同一个像素点）；
- 按压时长叠加 ±noise 的高斯抖动，模拟人手误差。
"""

from __future__ import annotations

import random
import math
import time

import pyautogui

pyautogui.FAILSAFE = True   # 鼠标甩到左上角可紧急停止
pyautogui.PAUSE = 0.01


def sample_landing_interval(cfg: dict) -> float:
    """Sample a new landing-to-takeoff interval for each jump."""
    value = cfg.get('landing_to_takeoff_seconds', [3.5, 5.0])
    if isinstance(value, (int, float)):
        low = high = float(value)
    else:
        low, high = map(float, value)
    if not (math.isfinite(low) and math.isfinite(high) and 0 < low <= high):
        raise ValueError('落地至起跳间隔必须为正数，且下限不能大于上限。')
    return random.uniform(low, high)


def press_and_hold(x: int, y: int, duration_ms: float,
                   release_at: float | None = None, region=None) -> float:
    """在屏幕物理坐标 (x, y) 处按下，按住 duration_ms 毫秒后松开。"""
    def check_window():
        if region is None or not region.hwnd:
            return
        import win32gui
        if win32gui.GetForegroundWindow() != region.hwnd:
            raise RuntimeError("游戏窗口不在前台，已暂停本次跳跃。")
        left, top = win32gui.ClientToScreen(region.hwnd, (0, 0))
        if left != region.left or (region.client_top is not None and top != region.client_top):
            raise RuntimeError("游戏窗口在等待期间移动，已停止；请重新运行。")
    if release_at is not None and time.monotonic() + duration_ms/1000.0 >= release_at:
        raise RuntimeError("已错过计划蓄力时间，取消本次点击；请重新运行。")
    check_window()
    pyautogui.moveTo(x, y, _pause=False)
    if release_at is not None:
        # Move first, then schedule the actual button-down time. PyAutoGUI
        # ignores moves shorter than MINIMUM_DURATION; do not assume a delay.
        start_at = release_at - duration_ms / 1000.0
        while time.monotonic() < start_at:
            pyautogui.failSafeCheck()
            time.sleep(min(0.025, max(0.0, start_at-time.monotonic())))
    check_window()
    if release_at is not None and release_at-time.monotonic() < 0.08:
        raise RuntimeError("距离计划起跳时间过短，取消本次点击。")
    pyautogui.mouseDown(_pause=False)
    pressed_at = time.monotonic()
    try:
        if release_at is None:
            _precise_sleep(duration_ms / 1000.0)
        else:
            _precise_sleep(max(0.0, release_at-time.monotonic()))
    finally:
        # Release even when the pointer has reached the fail-safe corner.
        previous_failsafe = pyautogui.FAILSAFE
        try:
            pyautogui.FAILSAFE = False
            pyautogui.mouseUp(_pause=False)
            released_at = time.monotonic()
        finally:
            pyautogui.FAILSAFE = previous_failsafe
    pyautogui.failSafeCheck()
    return (released_at-pressed_at)*1000.0


def _precise_sleep(seconds: float) -> None:
    """毫秒级延时：先普通 sleep，末尾自旋校准，降低系统调度抖动。"""
    if seconds <= 0:
        return
    end = time.perf_counter() + seconds
    if seconds > 0.02:
        time.sleep(seconds - 0.015)
    while time.perf_counter() < end:
        pass


def random_press_point(region, fixed: tuple[int, int] | None = None):
    """按压点：优先用配置固定点，否则在游戏区下半部随机取点。"""
    if fixed:
        return fixed
    x = random.randint(int(region.left + region.width * 0.3),
                       int(region.left + region.width * 0.7))
    y = random.randint(int(region.top + region.height * 0.6),
                       int(region.top + region.height * 0.8))
    return x, y


def predict_press_duration(distance_px: float, cfg: dict) -> float:
    """Predict charging time with a slope and an optional fixed latency."""
    distance_px = float(distance_px)
    slope = float(cfg['press_coefficient'])
    offset = float(cfg.get('press_offset_ms', 0.0))
    if not all(math.isfinite(x) for x in (distance_px, slope, offset)) or distance_px <= 0 or slope <= 0:
        raise ValueError('跳跃距离或按压模型参数无效。')
    duration = max(80.0, distance_px*slope+offset)
    if duration > cfg.get('max_press_ms', 1800):
        raise ValueError('识别距离或按压时间异常，已停止。')
    return duration


def jump(region, distance_px: float, cfg: dict,
         release_at: float | None = None) -> float:
    """执行一次跳跃，返回实际按压毫秒数。"""
    duration = predict_press_duration(distance_px, cfg)
    noise = duration * cfg.get("press_noise_ratio", 0.0)
    duration += random.gauss(0, noise)
    duration = max(80.0, duration)  # 下限，避免误触成单击
    if duration > cfg.get("max_press_ms", 1800):
        raise ValueError("识别距离或按压时间异常，已停止。")

    fixed = tuple(cfg["press_point"]) if cfg.get("press_point") else None
    x, y = random_press_point(region, fixed)
    return press_and_hold(x, y, duration, release_at, region=region)
