# 微信跳一跳 PC 端自动辅助

[English](README_EN.md) | 中文

通过截取 PC 微信「跳一跳」小程序窗口画面，OpenCV 识别棋子与目标方块，
计算像素距离换算按压时长，模拟鼠标长按完成自动跳跃。

## 项目结构

| 文件 / 目录 | 用途 |
|---|---|
| `main.py` | 主程序：定位窗口 → 截屏 → 识别 → 换算按压时长 → 模拟鼠标长按，循环跳跃 |
| `jump/vision.py` | 核心识别：头部小球锚定棋子位置；逐行段宽跟踪 + 平台停涨点（dw8 判据）定目标顶面中心；等距轴向投影距离计算 |
| `jump/control.py` | 鼠标控制：毫秒级精度的长按模拟，按压点随机化 + 时长抖动（防检测） |
| `jump/winutil.py` | 窗口工具：DPI 感知、按标题关键字找窗口、裁剪客户区坐标 |
| `config.json` | 全部可调参数（系数、轴向角、颜色范围、阈值等），运行中每跳重读、改完即生效 |
| `test_offline.py` | 离线回归测试：用 `samples/` 冻结样本验证识别算法不回归 |
| `samples/` | 13 张真实对局截图（覆盖粉/米/绿/灰/蓝背景，方盒/圆桌/圆柱/魔方/高台等形状） |
| `debug/` | 运行时生成的每跳标注图（棋子红圈、目标绿圈），排查识别问题用，不入库 |

## 环境

- Windows 10/11 + PC 微信（小程序面板可直接玩跳一跳）
- Python 3.9+

```bash
pip install -r requirements.txt
```

## 使用

1. PC 微信打开跳一跳小程序，进入对局（棋子站在方块上），窗口保持可见。
2. 运行：

   ```bash
   python main.py          # 连续跳，Ctrl+C 停止
   python main.py --once   # 只跳一步，调参时用
   ```

运行中把鼠标甩到屏幕左上角可紧急停止（pyautogui FailSafe）。
若落点系统性偏远/偏近，直接改 `config.json` 里的 `press_coefficient`
（偏远调小、偏近调大，每次 ±0.02 微调）。

## 配置说明（config.json）

| 字段 | 含义 |
|---|---|
| `window_title_keywords` | 游戏窗口标题关键字，按顺序匹配 |
| `title_bar_height` | 窗口标题栏像素高度，用于裁剪客户区 |
| `press_coefficient` | **核心参数**：像素距离 → 按压毫秒 的系数 |
| `jump_axis_angles_deg` | 等距轴向角度 `[30, 150]`：跳跃距离取位移在两轴上投影的最大值而非欧氏连线长（删掉该行则回退为欧氏距离） |
| `press_point` | 固定按压点 `[x, y]`；为 `null` 时在窗口下半部随机取点 |
| `under_game_score_ratio` | 分数区域高度占画面比例（扫描方块时跳过） |
| `piece_color` | 棋子颜色 RGB 范围，识别不准时调整 |
| `bg_diff_threshold` | 方块与背景色差阈值 |
| `press_noise_ratio` | 按压时长随机抖动比例（防检测） |
| `jump_wait_seconds` | 每跳后等待动画的时间区间 |
| `max_score_stop` | 跳跃步数上限，达到后主动停止 |
| `debug_save_screenshots` | 是否保存每跳标记图到 `debug/` |

## 常见问题

- **点击位置偏 / 截图坐标不对**：确认程序已调用 DPI 感知（`main.py` 已内置）；
  若 Windows 缩放为 125%/150%，务必通过 `python main.py` 启动而不是其他宿主。
- **找不到窗口**：`跳一跳` 窗口标题可能不同，改 `window_title_keywords`。
- **跳远/跳近**：微调 `config.json` 的 `press_coefficient`（偏远调小、
  偏近调大，每次 ±0.02）；近距离普遍偏远的话需要分段系数（后续增强项）。
- **识别失败**：看 `debug/` 里的标记图，调 `piece_color` 和
  `bg_diff_threshold`。
- **防检测**：程序已内置按压点随机化和时长抖动；分数不要刷太高，
  `max_score_stop` 建议 ≤ 1000。

## 参考

- 算法思路：[wangshub/wechat_jump_game](https://github.com/wangshub/wechat_jump_game)
- PC 端参考：[B5DX/pc_wechat_jump_auto](https://github.com/B5DX/pc_wechat_jump_auto)、
  [XDDXMT/AutoJump](https://github.com/XDDXMT/AutoJump)


## 落地验证、按压补偿和间隔控制

程序会在每跳后等待棋子及画面稳定，并跟踪目标平台随镜头移动后的位置，测量落点偏差。模板匹配只在落地棋子附近搜索；小平台会额外保留下方瓶身/底座特征，减少棋子遮挡瓶盖或相似白色平台导致的误停。无法确认落地、窗口移位/失焦、结算页或异常偏差时停止，不继续使用旧坐标点击。

按压时间支持 `距离 × press_coefficient + press_offset_ms`。默认固定补偿为 0，保留原来的系数口径；`config.windows-608.json` 是在 608×1085 物理像素游戏区域上实测得到的示例，并非通用参数。可先运行：

```bash
python main.py --snap
python main.py --once
python main.py --config config.windows-608.json --once
```

新增/更新的配置：

| 字段 | 作用 |
|---|---|
| `landing_to_takeoff_seconds` | `[3.5, 5.0]` 表示每跳独立抽取间隔；也接受单个正数作为固定间隔。蓄力包含在间隔内。 |
| `press_offset_ms` | 固定按压补偿，单位毫秒，默认 0。 |
| `press_model` | `affine` 使用带固定补偿的模型；在线更新只小幅调整补偿，保留批量拟合系数。 |
| `auto_calibrate` | 是否把高置信度落点用于更新配置，默认关闭；示例配置开启。 |
| `max_press_ms` | 异常按压时长上限，默认 1800 毫秒。 |
| `max_score_stop` | 每次运行的跳跃步数上限，默认 300；该字段不是实际游戏分数。 |

落地时间根据连续截图的稳定状态估计，不是读取游戏内部物理事件。间隔随机化不保证避免游戏检测。首跳仍使用 3 秒准备时间。

诊断图片按运行时间保存到 `debug/` 子目录，避免后续对局覆盖；`calibration.jsonl` 记录实际按压时长、计划/实际间隔、坐标及原图路径。`--snap` 即使关闭常规调试图片，也会保存一张标记图。在线校准只接受低模板误差、横向偏差较小的观测，异常记录不会更新参数。

### 离线验证

```bash
python test_offline.py
python -m unittest -v test_calibration
```

合成场景覆盖相似平台误匹配和棋子遮住小瓶盖；时序测试使用模拟时钟/鼠标，不会操作桌面。新增测试不需要用户截图或对局日志。示例补偿参数不代表跨设备准确率。
