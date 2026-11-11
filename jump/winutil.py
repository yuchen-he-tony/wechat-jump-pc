"""Windows 窗口工具：DPI 感知 + 游戏窗口定位。

要点：
- 必须在截屏和鼠标操作之前调用 set_dpi_aware()，
  否则 125%/150% 缩放下坐标系不一致，点击会偏。
- PC 微信的小程序面板是独立窗口（标题通常为小程序名，
  如「跳一跳」），通过标题关键字匹配。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

import pygetwindow as gw


def set_dpi_aware() -> None:
    """让进程使用物理像素坐标，消除 DPI 缩放偏差。"""
    if sys.platform != "win32":
        return
    try:
        # Windows 10 1703+，最可靠
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


@dataclass
class GameRegion:
    """游戏画面在屏幕上的物理像素区域。"""
    left: int
    top: int
    width: int
    height: int
    hwnd: int | None = None
    client_top: int | None = None

    def to_mss(self) -> dict:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}


def find_window(title_keywords: list[str]):
    """按标题关键字找到游戏窗口，返回 pygetwindow 窗口对象或 None。"""
    for kw in title_keywords:
        for win in gw.getAllWindows():
            title = (win.title or "").strip()
            if title and kw in title and win.width > 200 and win.height > 300:
                return win
    return None


def game_region_from_window(win, title_bar_height: int) -> GameRegion:
    """由窗口算出游戏客户区。

    优先用 win32 ClientToScreen + GetClientRect：pygetwindow 的
    win.left/top 包含 Windows 隐形边框，会把窗口外两侧的其他
    应用画面截进来（污染识别）。拿不到句柄时回退旧逻辑。
    注意：PC 微信的自定义标题栏在客户区内，仍需 title_bar_height 裁剪。
    """
    hwnd = getattr(win, "_hWnd", None)
    if hwnd:
        try:
            import win32gui
            left, top = win32gui.ClientToScreen(hwnd, (0, 0))
            _, _, cw, ch = win32gui.GetClientRect(hwnd)
            if cw > 200 and ch > 300:
                return GameRegion(left, top + title_bar_height,
                                  cw, ch - title_bar_height, hwnd, top)
        except Exception:
            pass
    return GameRegion(win.left, win.top + title_bar_height,
                      win.width, win.height - title_bar_height, hwnd)


def locate_game_region(title_keywords: list[str],
                       title_bar_height: int) -> GameRegion | None:
    """一键定位游戏区域；找不到窗口返回 None。"""
    win = find_window(title_keywords)
    if win is None:
        return None
    try:
        if win.isMinimized:
            win.restore()
        win.activate()  # 游戏窗口需要前台才能接收点击
    except Exception:
        pass
    return game_region_from_window(win, title_bar_height)
