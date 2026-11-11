"""截屏与图像识别：棋子底部中心 + 目标方块顶面中心。

识别算法沿用经典 wangshub/wechat_jump_game 思路，改为 numpy 向量化实现：
- 棋子：按固定颜色范围（深灰蓝色）做掩码，取掩码像素的最低点；
  棋子中心 x = 掩码像素的平均 x，落点 y = 最低点 y - 少量偏移。
- 方块：从分数区域以下逐行扫描，找出与行首背景色差异超过阈值的像素行，
  该行即方块上顶点所在行；在该行向左右扩展求菱形顶面宽度，
  中心 y ≈ 顶点 y + 半宽 * tan(30°)（等距视角几何关系）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import mss
import numpy as np

TAN_30 = math.tan(math.radians(30))  # ≈ 0.577


def axis_projected_distance(dx: float, dy: float,
                            angles_deg=(30.0, 150.0)) -> float:
    """位移向量在等距轴向上的投影绝对值的最大值。

    跳一跳世界是等距投影，下一个方块总是沿两条轴（实测 30°/150°）
    之一放置，跳跃方向因此总是贴着某条轴。跳跃距离取位移在两轴上
    投影的最大值，而不是欧氏连线长。
    """
    best = 0.0
    for a in angles_deg:
        rad = math.radians(a)
        best = max(best, abs(dx * math.cos(rad) + dy * math.sin(rad)))
    return best


@dataclass
class DetectResult:
    piece_x: int
    piece_y: int
    board_x: int
    board_y: int

    @property
    def distance(self) -> float:
        return math.hypot(self.board_x - self.piece_x,
                          self.board_y - self.piece_y)

    def axis_distance(self, angles_deg=(30.0, 150.0)) -> float:
        """棋子→目标的轴向投影距离（跳跃按压所用的距离口径）。"""
        return axis_projected_distance(self.board_x - self.piece_x,
                                       self.board_y - self.piece_y,
                                       angles_deg)


class ScreenCapturer:
    def __init__(self):
        self._sct = mss.mss()

    def grab_bgr(self, region) -> np.ndarray:
        """截取 GameRegion 区域，返回 HxWx3 的 BGR 数组。"""
        shot = self._sct.grab(region.to_mss())
        img = np.asarray(shot)          # BGRA
        return img[:, :, :3].copy()


def _color_mask(bgr: np.ndarray, color_cfg: dict) -> np.ndarray:
    """按 config 中 RGB 范围生成掩码（注意数组是 BGR 顺序）。

    可选 b_minus_r_min：要求 B 通道比 R 至少大该值。
    棋子是暗紫色（B>R>G），灰色杂物 R≈G≈B，可借此区分。
    """
    b_lo, b_hi = color_cfg["b"]
    g_lo, g_hi = color_cfg["g"]
    r_lo, r_hi = color_cfg["r"]
    mask = ((bgr[:, :, 0] >= b_lo) & (bgr[:, :, 0] <= b_hi)
            & (bgr[:, :, 1] >= g_lo) & (bgr[:, :, 1] <= g_hi)
            & (bgr[:, :, 2] >= r_lo) & (bgr[:, :, 2] <= r_hi))
    b_minus_r_min = color_cfg.get("b_minus_r_min")
    if b_minus_r_min is not None:
        diff = bgr[:, :, 0].astype(np.int16) - bgr[:, :, 2].astype(np.int16)
        mask &= diff >= b_minus_r_min
    return mask


def find_piece(bgr: np.ndarray, piece_color: dict,
               scan_start_ratio: float) -> tuple[int, int] | None:
    """找棋子底部中心。返回 (piece_x, piece_y) 或 None。

    以"头部小球"为锚（头的形状最规则、永不被底座污染）：
    1. 颜色范围 + b_minus_r_min（棋子暗紫 B>R，灰色底座 B≈R）；
    2. 行宽过滤：宽于棋子最大宽度的行判定为方块底座，整行剔除；
    3. 列聚类后逐段做垂直断行切分（>15px 空行断开），
       找"圆形头"（高 22~65、宽 26~62、高宽比 0.5~1.8）；
    4. 头下方 40px 内有连续身体段才采信——纯靠"最高段"会被
       紫背景下阴影/盒子描边连成的垃圾长段骗过（实测偏差 300px+）；
    5. 脚底 = 头 + 连续身体链的底部；身体被遮挡时按头高比例下移
       估算（头到脚 ≈ 10.8% 屏高）。piece_x 取脚底窄带均值。
    """
    h, w = bgr.shape[:2]
    mask = _color_mask(bgr, piece_color)
    mask[: int(h * scan_start_ratio), :] = False   # 跳过分数区域
    mask[int(h * 0.85):, :] = False                # 棋子不会出现在画面最底部
    mask[:, : int(w * 0.03)] = False               # 排除左右边缘杂带
    mask[:, int(w * 0.97):] = False

    max_piece_w = max(int(w * 0.09), 12)
    row_counts = mask.sum(axis=1)
    mask &= (row_counts <= max_piece_w)[:, None]   # 剔除宽行（方块底座）

    if mask.sum() < 50:
        return None

    # 列聚类：把掩码按列的连续段分组（允许 5px 缺口）
    cols = np.nonzero(mask.any(axis=0))[0]
    segments: list[tuple[int, int]] = []
    start = prev = int(cols[0])
    for c in cols[1:]:
        if c - prev > 5:
            segments.append((start, prev))
            start = int(c)
        prev = int(c)
    segments.append((start, prev))

    # 逐列段找"头 + 身体"结构，取像素面积最大者。
    # 头和身体是连通的一段（中间没有空行），所以不能用"圆形独立 run"
    # 找头——要在一段的顶部宽度剖面里找"脖子"：头宽涨到峰值后收窄
    # 到 55% 以下的位置就是头/颈分界。
    best = None  # (area, a, b, head_top, foot_y, head_cx, body_seen)
    for a, b in segments:
        seg = mask[:, a: b + 1]
        row_w = seg.sum(axis=1)
        s_ys = np.nonzero(row_w > 0)[0]
        if len(s_ys) < 20:
            continue
        splits = np.nonzero(np.diff(s_ys) > 15)[0]
        runs = [r for r in np.split(s_ys, splits + 1) if len(r) >= 3]

        for i, r in enumerate(runs):
            head_top = int(r.min())
            # 头校验：run 顶部 70 行内，宽度峰值 26~62px（头的直径），
            # 且在 18~70 行处收窄到峰值 55% 以下（脖子）
            top_rows = r[r - head_top < 70]
            if len(top_rows) < 18:
                continue
            prof = row_w[top_rows].astype(np.float32)
            peak = prof.max()
            if not (26 <= peak <= 62):
                continue
            neck = np.nonzero(prof < peak * 0.55)[0]
            neck = neck[neck >= 18]
            if len(neck) == 0:
                continue
            neck_at = int(neck[0])
            if neck_at > 70:
                continue
            # 头中心 x：头部各行掩码均值的平均（比脚底稳，身体可能被挡）
            head_rows = r[: neck_at + 1]
            cx_per_row = [np.mean(np.nonzero(seg[y])[0])
                          for y in head_rows if seg[y].any()]
            head_cx = a + int(np.mean(cx_per_row)) if cx_per_row \
                else (a + b) // 2

            # 身体链：本 run 及下方间隙 <= 40px 的连续 run
            # （遮挡/抗锯齿会断行；盒底角残渣间隙 > 80px，不会被并入）
            foot_y = int(r.max())
            area = int(seg[r].sum())
            j = i + 1
            while j < len(runs) and runs[j].min() - foot_y <= 40:
                foot_y = int(runs[j].max())
                area += int(seg[runs[j]].sum())
                j += 1
            body_seen = foot_y - head_top >= h * 0.05
            if not body_seen:
                # 身体被完全遮挡：按头到脚的比例下移估算
                foot_y = head_top + int(h * 0.108)
            if best is None or area > best[0]:
                best = (area, a, b, head_top, foot_y, head_cx, body_seen)
            break  # 每段只认最上面的头

    if best is None:
        # 兜底：全局最低点 + 底部带均值（旧逻辑）
        ys, xs = np.nonzero(mask)
        if len(ys) == 0:
            return None
        piece_y = int(ys.max())
        band = mask[max(piece_y - 12, 0): piece_y + 1]
        _, b_xs = np.nonzero(band)
        if len(b_xs) == 0:
            return None
        return int(b_xs.mean()), piece_y

    _, a, b, head_top, foot_y, head_cx, body_seen = best
    seg = mask[:, a: b + 1]
    if body_seen:
        # 脚底可见：取底部窄带均值，抗底座残连
        band = seg[max(foot_y - 12, 0): foot_y + 1]
        _, b_xs = np.nonzero(band)
        piece_x = int(a + b_xs.mean()) if len(b_xs) else head_cx
    else:
        piece_x = head_cx
    return piece_x, foot_y


def background_model(bgr: np.ndarray) -> np.ndarray:
    """估计背景色垂直渐变，返回每行的背景色 (h, 3)。

    游戏背景是平滑的垂直渐变色。逐行中位数在"障碍物+阴影覆盖过半"
    的行会被污染，所以只取画面上方的干净带（8%~26% 屏高处，分数数字
    占比很小不影响中位数）做线性拟合，再外推到全图。
    两pass：先拟合，剔除偏差大的行再拟合一次，防干净带里混入方块。
    """
    h = bgr.shape[0]
    img = bgr.astype(np.float32)
    y0, y1 = int(h * 0.08), int(h * 0.26)
    band_rows = np.median(img[y0:y1], axis=1)      # (n, 3) 每行中位色
    ys = np.arange(y0, y1, dtype=np.float32)

    def fit(rows, ys_):
        return [np.polyfit(ys_, rows[:, c], 1) for c in range(3)]

    coefs = fit(band_rows, ys)
    # 剔除离群行后重新拟合
    pred = np.stack([np.polyval(c, ys) for c in coefs], axis=1)
    keep = np.abs(band_rows - pred).sum(axis=1) < 30
    if keep.sum() >= 5:
        coefs = fit(band_rows[keep], ys[keep])

    all_y = np.arange(h, dtype=np.float32)
    bg = np.stack([np.polyval(c, all_y) for c in coefs], axis=1)
    return bg.astype(np.float32)


def _row_obstacle_segments(img: np.ndarray, y: int, bg_rows: np.ndarray,
                           piece_x: int,
                           bg_diff_threshold: int, gap: int = 25,
                           exclude_piece: bool = True,
                           bridge: tuple[int, int] | None = None):
    """提取某行的障碍物水平段列表 [(l, r), ...]。

    - 背景参照：全局渐变模型 bg_rows[y]（比行中位数抗污染）
    - 阴影过滤：像素 ≈ 背景 × k（0.65<k<0.96 且三通道比例一致）判为地面阴影。
      下限取 0.65 而非 0.5：浅灰背景下灰色方块顶面是"均匀变暗 ~0.54"，
      与地面阴影（实测 0.68~0.93）颜色特征几乎相同，0.5 会把方块误杀
    - exclude_piece=True 时排除棋子所在列（找顶角时防棋子头误判）；
      宽度跟踪时关掉，否则棋子遮挡会把目标块右侧挖掉、拉偏中心
    - bridge=(bx0, bx1)：被抹除的棋子区域，段落缺口若落在该区间内
      则桥接合并（棋子遮挡目标块时保持其宽度完整）
    - 排除左右 3% 边缘
    - 段内允许 <= gap 的缺口（桥接顶面图案，如白点、唱片）
    """
    h, w = img.shape[:2]
    row = img[y]
    bg = bg_rows[y]
    diff = np.abs(row - bg).sum(axis=1)
    hit = np.nonzero(diff > bg_diff_threshold)[0]
    if len(hit) == 0:
        return []
    px = row[hit].astype(np.float32)
    ratio = px / np.maximum(bg, 1.0)
    mean_r = ratio.mean(axis=1)
    uniform = (ratio.max(axis=1) - ratio.min(axis=1)) < 0.06
    shadow = uniform & (mean_r > 0.65) & (mean_r < 0.96)
    hit = hit[~shadow]
    keep = (hit > w * 0.03) & (hit < w * 0.97)
    if exclude_piece:
        keep &= np.abs(hit - piece_x) > w * 0.06
    hit = hit[keep]
    if len(hit) == 0:
        return []
    splits = np.nonzero(np.diff(hit) > gap)[0]
    groups = np.split(hit, splits + 1)
    segs = [(int(g[0]), int(g[-1])) for g in groups]
    if bridge is not None and len(segs) > 1:
        bx0, bx1 = bridge
        merged = [segs[0]]
        for l, r in segs[1:]:
            pl, pr = merged[-1]
            # 缺口完全落在被抹除的棋子区间内 → 是遮挡，桥接
            if pr >= bx0 - 10 and l <= bx1 + 10 and l - pr <= (bx1 - bx0) + 20:
                merged[-1] = (pl, r)
            else:
                merged.append((l, r))
        segs = merged
    return segs


def analyze_board(bgr: np.ndarray, piece_pos: tuple[int, int],
                  under_game_score_ratio: float,
                  bg_diff_threshold: int) -> dict | None:
    """分析目标方块顶面几何，失败返回 None。返回 dict：
    center=(x,y) 顶面中心；vertex=(x,y) 顶角；left/right=(x,y) 中心行
    左右端点（菱形左右角 / 椭圆最宽处两端）；plateau=是否经平台确立。

    方法：最大宽度行法 ——
    菱形/椭圆顶面的水平宽度都在"中心行"达到最大。从顶角开始逐行
    跟踪与上一行水平重叠的物体段；宽度显著收缩（< 85% 最大值）
    说明顶面已经结束（下方是底座/盒身/地面阴影），停止跟踪。
    board_y = 宽度最大行；board_x = 接近最大宽度的各行段中点的中位数。
    对带图案（唱片、白点、凹槽）的顶面鲁棒，也不受地面阴影桥接影响。
    """
    h, w = bgr.shape[:2]
    piece_x, piece_y = piece_pos
    img = bgr.astype(np.int16)
    bg_rows = background_model(bgr)

    # 抹掉棋子（含头顶小球）：防止棋子被误当顶点、防止棋子遮挡
    # 把目标块"挖洞"拉偏中心。用背景渐变色填充棋子包围盒。
    ph = int(h * 0.14)
    pw = int(w * 0.06)
    ey0, ey1 = max(piece_y - ph, 0), min(piece_y + 6, h)
    ex0, ex1 = max(piece_x - pw, 0), min(piece_x + pw, w)
    img[ey0:ey1, ex0:ex1] = bg_rows[ey0:ey1, None, :].astype(np.int16)

    y_start = int(h * under_game_score_ratio)
    y_end = min(piece_y, int(h * 0.9))  # 只扫棋子以上区域

    def track(vx: int, vy: int):
        """从候选顶角逐行跟踪物体段，返回
        (widths, mids, lefts, rights, plateau_idx, plateau_w)。

        plateau_idx / plateau_w：平台（左右边缘连续 8 行静止、且 8 行
        宽度增量 <=3 即宽度确实停涨）确立时，窗口起始行下标和宽度快照，
        未确立为 None。中心行 = plateau_idx + 7（窗口末行 = 停涨点）。
        dw8 判据防止椭圆顶面（圆桌/圆柱）接近中心时边缘移动 <1px/行
        造成的假平台：椭圆慢速区宽度仍在涨（8 行可涨 15px+），
        只有真正停涨（椭圆最宽行 / 方盒侧面起点）才确立。
        """
        scan_end = min(vy + int(h * 0.30), h - 1)
        prev_l, prev_r = vx - 2, vx + 2
        widths, mids, lefts, rights = [], [], [], []
        running_max = 0
        flat = 0
        plateau_w: float | None = None  # 平台宽度快照（只取第一次确立）
        plateau_idx: int | None = None
        empty = 0
        for y in range(vy, scan_end):
            segs = _row_obstacle_segments(img, y, bg_rows, piece_x,
                                          bg_diff_threshold,
                                          exclude_piece=False,
                                          bridge=(ex0, ex1))
            overlap = [(l, r) for l, r in segs
                       if r >= prev_l - 15 and l <= prev_r + 15]
            if not overlap:
                empty += 1
                if empty > 2:      # 容忍 2 行空洞（抗锯齿断行）
                    break
                widths.append(widths[-1] if widths else 0)
                mids.append(mids[-1] if mids else vx)
                lefts.append(prev_l)
                rights.append(prev_r)
                continue
            empty = 0
            l = min(s[0] for s in overlap)
            r = max(s[1] for s in overlap)
            wd = r - l
            # 段边缘被抹除区截断后重新伸出（遮挡解除），宽度会跳升。
            # 尚未确立平台时（顶面还在生长期被棋子挡住一部分）这是合法的，
            # 放行继续跟踪；平台确立后再跳升说明顶面数据已完整，
            # 跳升后的段已是盒身/邻物，必须停（否则中心被拉偏）
            zone_exit = (
                abs(l - prev_l) <= 10 and ex0 - 8 <= prev_r <= ex1 + 15
                and r > prev_r + 20
            ) or (
                abs(r - prev_r) <= 10 and ex0 - 15 <= prev_l <= ex1 + 8
                and l < prev_l - 20
            )
            if plateau_w is not None and wd > plateau_w * 1.05:
                # 平台（盒侧面）之后还在增宽 = 顶面数据已完整，增宽的
                # 段是盒身外扩/锥形底座/邻物，必须停（否则中心被拉偏）。
                # 椭圆顶面的"慢速区"误确立平台已在确立处用 dw8 判据防住，
                # 这里无需区分真假平台
                break
            if running_max >= w * 0.05:  # 顶角初期翻倍式增长，不判粘连
                if wd > running_max * 1.3 \
                        and not (zone_exit and plateau_w is None):
                    break          # 暴增：必然与相邻物体粘连
                if wd < running_max * 0.85:
                    break          # 显著收缩：顶面结束
            widths.append(wd)
            mids.append((l + r) // 2)
            lefts.append(l)
            rights.append(r)
            # 平台判定：左右边缘都静止（<= 1px/行；菱形生长期边缘
            # 移动 1.73px/行，交替出现 1 和 2，不会连续 8 行静止）
            if abs(l - prev_l) <= 1 and abs(r - prev_r) <= 1 \
                    and running_max >= w * 0.05:
                flat += 1
                if flat == 8 and plateau_w is None:
                    # dw8 生长判据：真平台（方盒侧面）宽度在 8 行内
                    # 零增长；椭圆顶面（圆桌）接近中心时边缘移动虽
                    # <1px/行会骗过 flat 计数，但 8 行宽度仍累计增长
                    # 15px+。增量 >3 说明顶面仍在生长，不确立平台，
                    # flat 退回 7 让下一行用滑动窗口重试（椭圆正中
                    # 附近斜率趋近 0，dw8 会 <=3，在距中心仅几行处
                    # 确立，误差可接受）
                    if widths[-1] - widths[-8] <= 3:
                        plateau_w = float(max(widths[-8:]))
                        plateau_idx = len(widths) - 8
                    else:
                        flat = 7
            else:
                flat = 0
            prev_l, prev_r = l, r
            if wd > running_max * 1.02:
                running_max = wd
        return widths, mids, lefts, rights, plateau_idx, plateau_w

    # 1. 找顶角（候选可能被抗锯齿伪影/噪点欺骗，逐个验证）
    y = y_start
    while y < y_end:
        segs = _row_obstacle_segments(img, y, bg_rows, piece_x,
                                      bg_diff_threshold, gap=10,
                                      bridge=(ex0, ex1))
        if not segs:
            y += 1
            continue
        l, r = max(segs, key=lambda s: s[1] - s[0])
        vx, vy = (l + r) // 2, y

        widths, mids, lefts, rights, plateau_idx, plateau_w = track(vx, vy)
        if widths and max(widths) >= w * 0.08 and len(widths) >= 8:
            break  # 有效顶角
        y = vy + 1  # 伪顶角，继续向下找
    else:
        return None
    if not widths or max(widths) < w * 0.05:
        return None
    max_w = max(widths)

    # 2. 中心行：平台确立点即顶面最大宽度行（中心行）。
    #    平台的确立已在 track() 里加了 dw8 判据（8 行宽度增量 <=3 才
    #    确立），因此确立点天然就是"宽度停止增长"的位置：
    #    - 方盒：菱形生长期边缘移动 1.73px/行，flat 计数不会累计；
    #      到达中心行后侧面垂直向下（宽度零增长），dw8<=3 立刻确立。
    #      不能用 argmax——侧面抗锯齿爬升 + 地面阴影并入会把它拖到
    #      盒身底部（实测 0000 阴影会把段从 182 撑到 311）
    #    - 椭圆顶面（圆桌/圆柱）：接近中心时边缘移动 <1px/行会骗过
    #      flat 计数，但 dw8 判据（还在生长就 >3）会阻止提前确立，
    #      直到宽度真正停涨（椭圆最宽行）才确立
    #    - 无平台（金字塔/菱形顶、窄底座收缩出峰顶）：argmax 即中心行
    if plateau_idx is not None:
        # plateau_idx 是 8 行静止窗口的起点，确立点（中心行）在窗口末行
        est = plateau_idx + 7
        board_y = vy + est
        # 中心 x 用平台期各行中点的中位数（最稳定的满宽行）
        board_x = int(np.median(mids[plateau_idx: est + 1]))
    else:
        est = int(np.argmax(widths))
        board_y = vy + est
        # 中心 x = 达到最大宽度 97% 的各行段中点的中位数（抗单行噪声；
        #    棋子抹除区截断的行宽度不足，自然被排除）
        full_mids = [mids[i] for i, wd in enumerate(widths)
                     if wd >= max_w * 0.97]
        board_x = int(np.median(full_mids))
    return {
        "center": (board_x, board_y),
        "vertex": (vx, vy),
        # 中心行的左右端点（菱形左右角 / 椭圆最宽处两端）
        "left": (lefts[est], board_y),
        "right": (rights[est], board_y),
        "plateau": plateau_idx is not None,
    }


def find_board(bgr: np.ndarray, piece_pos: tuple[int, int],
               under_game_score_ratio: float,
               bg_diff_threshold: int) -> tuple[int, int] | None:
    """找目标方块顶面中心。返回 (board_x, board_y) 或 None。"""
    geo = analyze_board(bgr, piece_pos, under_game_score_ratio,
                        bg_diff_threshold)
    return geo["center"] if geo else None




def detect(bgr: np.ndarray, cfg: dict) -> DetectResult | None:
    """完整识别流程。失败返回 None。"""
    piece = find_piece(bgr, cfg["piece_color"], cfg["scan_start_ratio"])
    if piece is None:
        return None
    board = find_board(bgr, piece,
                       cfg["under_game_score_ratio"],
                       cfg["bg_diff_threshold"])
    if board is None:
        return None
    return DetectResult(piece[0], piece[1], board[0], board[1])


def is_playing_frame(bgr: np.ndarray) -> bool:
    """Require the dark score glyph at top left; settlement screens lack it."""
    h, w = bgr.shape[:2]
    roi = bgr[int(h*0.055):int(h*0.18), int(w*0.07):int(w*0.23)]
    if roi.size == 0:
        return False
    gray = roi.mean(axis=2)
    dark = (gray < np.median(gray)-45) & (gray < 130)
    ys, xs = np.nonzero(dark)
    return (len(ys) >= 60 and np.ptp(ys) >= 15 and np.ptp(xs) >= 4)


def annotate(bgr: np.ndarray, res: DetectResult) -> np.ndarray:
    """画标记的调试图（BGR）。"""
    import cv2
    out = bgr.copy()
    cv2.circle(out, (res.piece_x, res.piece_y), 8, (0, 0, 255), 2)
    cv2.circle(out, (res.board_x, res.board_y), 8, (0, 255, 0), 2)
    cv2.line(out, (res.piece_x, res.piece_y),
             (res.board_x, res.board_y), (255, 0, 0), 2)
    cv2.putText(out, f"dist={res.distance:.1f}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    return out
