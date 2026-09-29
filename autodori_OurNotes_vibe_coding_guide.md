# autodori / Our Notes 播放链路故障排查与修复指导

> 用途：Vibe Coding / Roo Code / Cursor 等代码代理的开发指导。
>
> 目标：修复当前 Our Notes 自动打歌启动后卡死在 `wait_first_note()` 的问题，并在此基础上检查截图坐标、触控坐标、旋转和 MAA Pipeline 的异步竞态。
>
> 本文基于当前仓库 `https://github.com/2293518845/YMZtestlog` 中的 `autodori.py`、`player.py`、`util.py`、`chart.py` 以及 `maafw.log`、`autodori-20260930-014228.log` 的现有证据。不要把推测当成已经证实的事实。

---

## 1. 当前问题结论

当前一次运行的关键时间线：

1. MaaFramework 正常启动，MuMu / ADB / minitouch 初始化成功。
2. Our Notes 成功识别歌曲 `春日影 (MyGO!!!!! ver.)`。
3. Our Notes 谱面成功解析，共 `237 notes`。
4. `SurfaceOrientation = 1`。
5. `SaveSong` 成功。
6. `OurNotes_WaitLoading` 成功识别。
7. MAA 调用自定义 `Play` Action。
8. Python 创建后台线程执行 `play_song()`，然后 `Play` Action 立即返回 `True`。
9. 播放线程进入 `wait_first_note()`。
10. 此后约每 100 ms 重复：
   `Exception in wait_first_note: 'NoneType' object is not subscriptable`
11. 没有进入正常的命令发布 / minitouch 打歌阶段。
12. 日志最后停止是用户手动关闭程序造成的，不应当视为程序自身崩溃。

因此，当前首次阻塞点是：

```text
play_song()
  -> wait_first_note()
     -> 截图/运行时参数/颜色检测路径
        -> TypeError: 'NoneType' object is not subscriptable
```

---

## 2. 第一优先级：定位 NoneType 的精确来源

不要直接猜测并大改播放算法。

当前最值得怀疑的是 `player.py` 的 MuMu IPC 截图路径：

```python
frame = self.player.ipc_capture_display(self.display_id)
return frame[:, :, :3]
```

如果 `ipc_capture_display()` 返回 `None`，这里会直接产生：

```text
TypeError: 'NoneType' object is not subscriptable
```

然后异常会被 `wait_first_note()` 捕获。

另一个理论可能是：

```python
get_runtime_info(current_player.resolution, GAME_TYPE)
```

收到 `None` resolution，或者 `get_color_eval_in_range()` 内部对 None 做下标操作。

目前不能仅凭已有日志排除这些可能性，因此必须先加入完整 traceback 和运行时值日志。

---

## 3. 必须首先修改的诊断代码

### 3.1 player.py：保护 MuMu IPC 截图

将 MuMu 截图逻辑改成类似：

```python
def ipc_capture_display(self):
    if self.type.startswith("mumu"):
        if self.display_id == -1:
            self.display_id = self.player.ipc_get_display_id(
                "com.bilibili.star.bili"
            )
            logging.debug(f"MuMu display_id = {self.display_id}")

        frame = self.player.ipc_capture_display(self.display_id)

        logging.debug(
            "MuMu IPC frame: "
            f"type={type(frame)}, "
            f"shape={getattr(frame, 'shape', None)}, "
            f"is_none={frame is None}"
        )

        if frame is None:
            return None

        return frame[:, :, :3]

    return self.player.capture()
```

注意：不要假设 `com.bilibili.star.bili` 一定是当前 Our Notes 游戏包名。必须确认这个包名是否正确。

---

### 3.2 autodori.py：把异常改成完整 traceback

不要继续：

```python
except Exception as e:
    logging.error(f"Exception in wait_first_note: {e}")
```

改成：

```python
except Exception:
    logging.exception("Exception in wait_first_note")
    time.sleep(0.1)
```

这样下一次运行可以直接得到具体 Python 文件、行号和调用栈。

---

### 3.3 wait_first_note：记录关键运行时信息

在进入循环前记录：

```python
logging.info(
    f"wait_first_note: resolution={current_player.resolution}, "
    f"orientation={current_orientation}, "
    f"game_type={GAME_TYPE}, "
    f"from_row={from_row}, "
    f"to_row={to_row}"
)
```

截图之后记录：

```python
logging.debug(
    "wait_first_note capture: "
    f"screen_type={type(screen)}, "
    f"screen_shape={getattr(screen, 'shape', None)}, "
    f"screen_is_none={screen is None}"
)
```

颜色检测之后记录：

```python
logging.debug(
    "wait_first_note color: "
    f"cur_color={cur_color}, "
    f"cur_color_type={type(cur_color)}"
)
```

不要长期每帧输出高频 debug 日志；定位完成后应删除或降低频率。

---

## 4. 关键分支的判断方式

下一次运行后根据 traceback 判断：

### 情况 A：异常发生在

```python
return frame[:, :, :3]
```

结论：

```text
MuMu IPC 返回 None
```

重点检查：

- `display_id`
- `ipc_get_display_id()`
- 当前游戏包名
- MuMu IPC 是否已经允许/支持 display capture
- 游戏窗口是否在 IPC capture 时可用

不要修改 `wait_first_note()` 算法来掩盖这个问题。

---

### 情况 B：异常发生在

```python
get_runtime_info(current_player.resolution, GAME_TYPE)
```

结论：

```text
current_player.resolution 很可能是 None 或格式不符合预期
```

检查：

```python
current_player.resolution
```

以及 resolution 是在哪里初始化、更新的。

---

### 情况 C：异常发生在

```python
cur_color[0:3]
```

结论：

```text
get_color_eval_in_range() 返回的 cur_color 可能是 None
```

需要检查：

```python
get_color_eval_in_range()
```

的所有 return 路径，以及传入的：

```text
screen
from_row
to_row
```

---

### 情况 D：异常变成 IndexError

如果修复截图问题后出现：

```text
IndexError: index ... is out of bounds for axis 0
```

那么优先检查 Our Notes 的 ROI 坐标。

---

## 5. 已经发现的坐标系风险

日志中存在三套重要尺寸：

```text
MAA 游戏截图：
1280 × 720

MuMu/minitouch：
720 × 1280

SurfaceOrientation：
1
```

这并不一定错误，因为旋转后宽高交换是合理的。

但是当前代码需要明确区分：

```text
截图坐标系
触控坐标系
谱面逻辑坐标系
```

不能把 `resolution` 当成一个无语义的 `(width, height)` 到处直接使用。

---

## 6. 当前 Our Notes ROI 的潜在问题

当前 `util.py` 的 Our Notes 配置仍带有类似：

```python
# 坐标配置（占位符，需根据实际游戏画面测量）
```

并且 `wait_first` 使用基于分辨率缩放的区域。

如果：

```python
resolution = (720, 1280)
```

而缩放基准是：

```text
1280 × 720
```

则纵向坐标可能被计算到：

```text
711 ~ 800
```

但横屏截图高度可能只有：

```text
720
```

于是 ROI 会越界。

因此不要简单地“把异常 catch 掉然后继续”。

必须验证：

```python
screen.shape
from_row
to_row
```

并确保：

```python
0 <= from_row < to_row <= screen.shape[0]
```

---

## 7. 建议给 get_color_eval_in_range() 增加边界保护

在确认函数接口后，可以采用类似思路：

```python
height = screen.shape[0]

from_row = max(0, min(from_row, height))
to_row = max(0, min(to_row, height))

if from_row >= to_row:
    logging.error(
        f"Invalid wait_first ROI: "
        f"from={from_row}, to={to_row}, screen_height={height}"
    )
    return None, None
```

但是：

**不要仅靠 clamp 掩盖坐标系错误。**

正确做法是先确认 Our Notes `wait_first` 的实际屏幕位置，再确定其坐标属于：

- 1280×720 横屏截图；
- 720×1280 触控坐标；
- 或经过 orientation 转换后的坐标。

---

## 8. SurfaceOrientation = 1 必须单独验证

当前日志：

```text
SurfaceOrientation: 1
```

同时：

```text
MAA screenshot = 1280×720
minitouch = 720×1280
```

需要建立明确的转换：

```text
Our Notes chart coordinate
        ↓
screen coordinate (1280×720)
        ↓
orientation transform
        ↓
touch coordinate (720×1280)
        ↓
minitouch
```

不要在多个地方分别做旋转。

建议最终只保留一个明确的坐标转换函数，例如：

```python
def chart_to_screen(x, y, screen_size, game_type):
    ...

def screen_to_touch(x, y, screen_size, orientation):
    ...
```

播放逻辑只调用：

```python
touch_x, touch_y = screen_to_touch(...)
```

避免在 `chart.py`、`util.py`、`autodori.py`、`player.py` 各自隐式旋转。

---

## 9. 不要把当前 NoneType 当成 timing / offset 问题

当前故障发生在：

```text
wait_first_note()
```

也就是：

```text
第一颗 note 同步
```

阶段。

因此目前：

```text
OFFSET
累计误差
CMD_SLICE_SIZE
actions_to_MNTcmd()
_adjust_offset()
_get_wait_time()
```

都还没有成为第一故障点。

不要先修改这些逻辑。

正确顺序是：

```text
截图可用
→ ROI 正确
→ 第一颗 note 检测成功
→ wait_first_note 返回
→ command builder 正常工作
→ minitouch 收到正确命令
→ 再验证 timing / offset
```

---

## 10. Play Action 存在独立的异步竞态

当前代码大致是：

```python
play_thread = threading.Thread(
    target=play_song,
    daemon=True
)

play_thread.start()

return CustomAction.RunResult(True)
```

这意味着：

```text
MAA：
Play Action
  ↓
线程启动
  ↓
立即 True
  ↓
继续 Pipeline
```

而 Python：

```text
play_song
  ↓
wait_first_note
  ↓
真正开始播放
```

两个流程没有同步。

所以即使修复当前 NoneType，MAA 仍可能在歌曲实际结束之前进入：

```text
OurNotes_SettleOverview
```

这会造成第二个问题。

---

## 11. 推荐的播放状态设计

不要让 MAA 只依赖：

```python
RunResult(True)
```

代表“线程已经创建”。

建议建立明确状态：

```python
PLAY_IDLE
PLAY_STARTING
PLAYING
PLAY_FINISHED
PLAY_FAILED
```

例如：

```python
play_state = "IDLE"
play_error = None
```

`play_song()`：

```python
play_state = "STARTING"

try:
    wait_first_note()

    play_state = "PLAYING"

    # actual playback

    play_state = "FINISHED"

except Exception as e:
    play_error = e
    play_state = "FAILED"
```

然后 MAA Action 应该根据实际设计决定：

```text
方案 1：
Play Action 阻塞到歌曲结束

方案 2：
Play Action 启动后台线程，但后续 Pipeline 增加 WaitPlayFinished

方案 3：
使用 MAA 自定义 Action 的同步机制等待播放状态
```

目前推荐优先采用“明确的播放状态 + 显式等待完成”，不要依赖线程启动即返回成功。

---

## 12. 失败状态也必须能够传播

当前：

```python
wait_first_note()
```

内部无限：

```python
while True:
    try:
        ...
    except Exception:
        sleep(0.1)
```

这是非常危险的。

任何永久性错误都会变成：

```text
错误
↓
吞掉
↓
100ms
↓
重试
↓
错误
↓
永久循环
```

应该区分：

```text
暂时性截图失败
永久性配置错误
坐标越界
IPC 不可用
游戏窗口消失
```

建议：

```python
MAX_CAPTURE_FAILURES = 30
```

超过次数：

```python
raise RuntimeError(...)
```

让上层知道播放失败。

---

## 13. wait_first_note 的推荐结构

目标结构：

```python
def wait_first_note():
    info = get_runtime_info(...)
    from_row = info["wait_first"]["from"]
    to_row = info["wait_first"]["to"]

    validate_wait_first_roi(...)

    last_color = None
    waited_frames = 0

    while True:
        screen = current_player.ipc_capture_display()

        if screen is None:
            handle_capture_failure()
            continue

        validate_screen_shape(screen)

        cur_color, score = get_color_eval_in_range(
            screen,
            from_row,
            to_row
        )

        if cur_color is None:
            handle_color_detection_failure()
            continue

        if last_color is not None:
            # compare color change

        last_color = cur_color

        # timeout / first-note detection
```

关键原则：

```text
不要让异常无限吞掉
不要让非法 ROI 进入 numpy
不要让 None 继续进入后续计算
```

---

## 14. 开发代理的具体执行顺序

让 Vibe Coding Agent 按以下顺序工作，不要一次性重构整个项目。

### Step 1

读取：

```text
autodori.py
player.py
util.py
chart.py
```

确认当前实际代码，而不是依据本文中的伪代码猜测。

### Step 2

找到：

```text
wait_first_note
get_runtime_info
get_color_eval_in_range
ipc_capture_display
Play
play_song
_ournotes_to_actions
actions_to_MNTcmd
```

### Step 3

只加入诊断日志：

```text
resolution
display_id
screen type
screen shape
ROI
cur_color
完整 traceback
```

### Step 4

运行一次。

根据 traceback 判断 None 的真实来源。

### Step 5

只修复真实来源。

不要提前修改 offset / timing。

### Step 6

确认：

```text
wait_first_note 成功返回
```

### Step 7

确认：

```text
第一批 minitouch command 被发送
```

### Step 8

记录第一批 note 的：

```text
chart x/y
screen x/y
touch x/y
orientation
```

### Step 9

人工验证点击位置是否正确。

### Step 10

最后才处理：

```text
Play Action / Pipeline race
```

---

## 15. 必须保留的诊断信息

修复过程中至少保留一次完整日志：

```text
resolution
screen.shape
SurfaceOrientation
display_id
wait_first ROI
第一颗 note 坐标
chart → screen 坐标
screen → touch 坐标
第一批 command
```

这样以后再出现“能运行但点歪”的问题时，可以直接判断是哪一层转换出了问题。

---

## 16. 不应该做的事情

不要：

```text
直接删除 wait_first_note()
```

不要：

```text
遇到异常直接 return True
```

不要：

```text
把 None 转成黑色图片
```

不要：

```text
无限 catch Exception
```

不要：

```text
直接把 720×1280 改成 1280×720
```

除非已经确认这个值的语义。

不要：

```text
看到 SurfaceOrientation=1 就直接交换所有 x/y
```

因为旋转通常还涉及：

```text
x' = ...
y' = ...
```

而不是简单交换。

不要在当前阶段先调整：

```text
OFFSET
BPM
note timing
CMD_SLICE_SIZE
```

---

## 17. 最终验收标准

第一阶段：

```text
程序启动
→ MAA 识别歌曲
→ 237 notes 解析成功
→ WaitLoading 成功
→ Play Action
→ wait_first_note 成功
```

第二阶段：

```text
第一批 command 成功发送
```

第三阶段：

```text
实际点击位置与 Our Notes 轨道一致
```

第四阶段：

```text
整首歌曲可以稳定完成
```

第五阶段：

```text
MAA 在 Python 播放真正结束后才进入结算流程
```

第六阶段：

```text
连续运行多首歌曲，不出现：
- NoneType 无限循环
- ROI 越界
- 坐标整体旋转
- 播放线程提前结束
- Pipeline 提前进入结算
```

---

## 18. 当前最重要的结论

当前不要把问题描述成“autodori 打歌 timing 不准”。

目前更准确的描述是：

```text
Our Notes 播放线程在进入第一颗 note 同步阶段时，
wait_first_note() 内部发生了
'NoneType' object is not subscriptable。

最可疑的具体代码路径是：

Player.ipc_capture_display()
    ↓
MuMu IPC 返回 None
    ↓
frame[:, :, :3]
    ↓
TypeError

同时必须排除：
current_player.resolution == None
以及
get_color_eval_in_range() 返回 None。

修复后还需要处理 1280×720 截图、
720×1280 触控设备和 SurfaceOrientation=1
之间的坐标转换问题。

最后还需要处理 Play Action
“启动后台线程后立即返回 True”
导致的 MAA Pipeline 异步竞态。
```

Vibe Coding Agent 应优先“证据驱动地定位 None 的来源”，再进行最小修复，不要直接进行大规模重构。
