"""微信跳一跳 PC 端自动辅助 —— 主程序。

用法：
    python main.py              # 全自动连续跳
    python main.py --once       # 只跳一步（调参时用）
    python main.py --config my_config.json

前置条件：
    1. PC 微信打开「跳一跳」小程序，窗口可见、不要最小化；
    2. 已进入游戏对局（棋子站在方块上）；
    3. 运行期间不要遮挡游戏窗口，鼠标甩到屏幕左上角可紧急停止。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import cv2
import pyautogui

from jump import calibration, control, vision, winutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_image(img, path: str) -> bool:
    """cv2.imwrite 在 Windows 下写不了含中文的路径（静默失败），
    改用 imencode + tofile。"""
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return False
    buf.tofile(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="微信跳一跳 PC 端自动辅助")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    parser.add_argument("--once", action="store_true", help="只跳一步")
    parser.add_argument("--snap", action="store_true",
                        help="只截一帧并保存识别标记图，不跳（诊断用）")
    args = parser.parse_args()

    cfg = load_config(args.config)
    winutil.set_dpi_aware()

    print("[*] 正在定位游戏窗口……")
    region = winutil.locate_game_region(cfg["window_title_keywords"],
                                        cfg["title_bar_height"])
    if region is None:
        print("[!] 找不到游戏窗口。请确认 PC 微信已打开跳一跳小程序，"
              "或修改 config.json 的 window_title_keywords。")
        return 1
    print(f"[*] 游戏区域: left={region.left} top={region.top} "
          f"{region.width}x{region.height}")

    run_id = time.strftime("%Y%m%d-%H%M%S")
    debug_dir = os.path.join(BASE_DIR, cfg.get("debug_dir", "debug"), run_id)
    os.makedirs(debug_dir, exist_ok=True)

    capturer = vision.ScreenCapturer()
    fails = 0
    jumps = 0
    last_landed_at = None
    log_path = os.path.join(BASE_DIR, "calibration.jsonl")

    print("[*] 3 秒后开始，请切换到游戏窗口……（Ctrl+C 停止）")
    time.sleep(3)

    while True:
        cfg = load_config(args.config)  # 每跳前重读配置，边跑边调不重启
        region = winutil.locate_game_region(cfg["window_title_keywords"],
                                            cfg["title_bar_height"])
        if region is None:
            print("[!] 游戏窗口已关闭，停止运行。")
            return 1
        frame = capturer.grab_bgr(region)
        if not vision.is_playing_frame(frame):
            os.makedirs(debug_dir, exist_ok=True)
            save_image(frame, os.path.join(debug_dir, 'not_in_game.png'))
            print("[!] 未确认左上角对局分数：可能为结算页、遮挡或加载页；不执行点击。")
            return 1
        res = vision.detect(frame, cfg)

        if res is None:
            fails += 1
            print(f"[!] 识别失败（连续 {fails} 次）")
            if fails >= cfg.get("fail_retry_limit", 8):
                print("[*] 连续识别失败，可能已游戏结束。共跳跃 "
                      f"{jumps} 步。")
                break
            time.sleep(0.8)
            continue
        fails = 0

        if cfg.get("debug_save_screenshots"):
            path = os.path.join(debug_dir, f"jump_{jumps:04d}.png")
            save_image(frame, os.path.join(debug_dir, f"jump_{jumps:04d}_raw.png"))
            if not save_image(vision.annotate(frame, res), path):
                print(f"[!] 调试图保存失败: {path}")

        if args.snap:
            os.makedirs(debug_dir, exist_ok=True)
            save_image(vision.annotate(frame, res),
                       os.path.join(debug_dir, f"jump_{jumps:04d}.png"))
            print(f"[*] 已保存标记图到 {debug_dir}\\jump_{jumps:04d}.png，"
                  "未执行跳跃。")
            break

        angles = cfg.get("jump_axis_angles_deg")
        dist = res.axis_distance(angles) if angles else res.distance
        tracker = calibration.BoardTracker.create(frame, res, cfg)
        planned_interval = (control.sample_landing_interval(cfg)
                            if last_landed_at is not None else None)
        release_at = (last_landed_at + planned_interval
                      if planned_interval is not None else None)
        if planned_interval is not None:
            print(f"[*] 本跳落稳至起跳计划间隔：{planned_interval:.3f}s")
        duration = control.jump(region, dist, cfg, release_at=release_at)
        released_at = time.monotonic()
        jumps += 1
        print(f"[+] 第 {jumps} 跳: 轴向距离 {dist:.1f}px"
              f"（欧氏 {res.distance:.1f}）→ 按压 {duration:.0f}ms")

        settled = calibration.wait_for_landing(capturer, region, cfg, released_at)
        if settled is None:
            print("[!] 无法确认棋子落稳，已停止。")
            break
        landing_frame, landed_piece, landed_at = settled
        if not vision.is_playing_frame(landing_frame):
            save_image(landing_frame, os.path.join(debug_dir, f'not_in_game_{run_id}.png'))
            print("[!] 落地后未确认对局画面，已停止；不更新校准。")
            break
        interval = (released_at-last_landed_at
                    if last_landed_at is not None else None)
        last_landed_at = landed_at
        measured_center = tracker.locate(landing_frame, near_piece=landed_piece) if tracker else None
        record = dict(run=run_id, step=jumps, distance=round(dist, 2),
                      duration_ms=round(duration, 2),
                      coefficient=cfg['press_coefficient'],
                      press_offset_ms=cfg.get('press_offset_ms', 0.0),
                      planned_interval_seconds=round(planned_interval, 3) if planned_interval else None,
                      landing_to_takeoff_seconds=round(interval, 3) if interval else None)
        record.update(piece_before=[res.piece_x, res.piece_y],
                      target_before=[res.board_x, res.board_y],
                      landed_piece=list(map(int, landed_piece)))
        if cfg.get('debug_save_screenshots'):
            raw_landing = os.path.join(debug_dir, f'landing_{jumps:04d}_raw.png')
            save_image(landing_frame, raw_landing)
            record['landing_image'] = os.path.relpath(raw_landing, BASE_DIR)
            record['before_image'] = os.path.relpath(
                os.path.join(debug_dir, f'jump_{jumps-1:04d}_raw.png'), BASE_DIR)
        if measured_center is not None:
            center = measured_center[:2]
            record['tracked_center'] = list(map(int, center))
            signed, norm = calibration.signed_error(res, landed_piece, center)
            record.update(error_along_px=round(signed, 2),
                          error_total_px=round(norm, 2),
                          template_loss=round(measured_center[2], 4))
            if norm > 65:
                record['valid'] = False
                record['reason'] = 'landing outside platform center tolerance'
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(record, ensure_ascii=False)+'\n')
                save_image(landing_frame, os.path.join(debug_dir, f'uncertain_{run_id}.png'))
                print(f"[!] 落点偏差 {norm:.1f}px，无法确认本次成功，已停止；不更新校准。")
                break
            record['valid'] = True
            label = "偏远" if signed > 0 else "偏近"
            print(f"[校准] {label} {abs(signed):.1f}px，中心偏差 {norm:.1f}px"
                  + (f"，落地至起跳 {interval:.2f}s" if interval else ""))
            landed_res = vision.DetectResult(*landed_piece, *center)
            if cfg.get('debug_save_screenshots'):
                save_image(vision.annotate(landing_frame, landed_res),
                           os.path.join(debug_dir, f"landing_{run_id}_{jumps:02d}.png"))
            lateral = max(0.0, norm*norm-signed*signed) ** 0.5
            if (cfg.get('auto_calibrate', False) and measured_center[2] < 0.07
                    and norm < dist*0.25 and lateral < 20
                    and dist+signed > 80):
                if cfg.get('press_model') == 'affine':
                    # Keep the batch-fitted slope stable; only make small
                    # latency corrections from reliable landing observations.
                    current = cfg.get('press_offset_ms', 0.0)
                    estimate = duration-cfg['press_coefficient']*(dist+signed)
                    estimate = max(current-10, min(current+10, estimate))
                    cfg['press_offset_ms'] = round(
                        max(-20, min(100, current*0.85+estimate*0.15)), 4)
                    record['next_offset_ms'] = cfg['press_offset_ms']
                else:
                    estimate = duration/(dist+signed)
                    estimate = max(cfg['press_coefficient']*0.90,
                                   min(cfg['press_coefficient']*1.10, estimate))
                    cfg['press_coefficient'] = round(
                        cfg['press_coefficient']*0.65+estimate*0.35, 5)
                    record['next_coefficient'] = cfg['press_coefficient']
                with open(args.config, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
        else:
            record['valid'] = False
            record['reason'] = 'landing platform tracking is uncertain'
            save_image(landing_frame, os.path.join(debug_dir, f'uncertain_{run_id}.png'))
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False)+'\n')
            print("[!] 未可靠跟踪到棋子附近的落地平台，已停止；不更新校准。")
            break
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False)+'\n')
        if args.once:
            break
        if jumps >= cfg.get("max_score_stop", 10 ** 9):
            print(f"[*] 达到设定的 {jumps} 步上限，主动停止。")
            break

    print("[*] 结束。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[*] 用户停止。")
    except pyautogui.FailSafeException:  # noqa: F821
        print("\n[*] 触发了 FailSafe（鼠标在左上角），紧急停止。")
