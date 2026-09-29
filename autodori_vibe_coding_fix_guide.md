# AutoDori / Our Notes 演奏线程故障修复指南

> 用途：将本次日志分析结论转换为可直接交给 Cursor、Roo Code、Claude Code 等 Vibe Coding 工具执行的开发任务说明。
>
> 仓库：`https://github.com/2293518845/YMZtestlog`
>
> 本文针对本次 2026-09-30 运行日志中已经确认的问题，不要求重构整个项目，也不要在没有证据的情况下修改谱面转换或时间算法。

## 1. 本次故障结论

本次运行已经成功完成：

1. MaaFramework 初始化。
2. MuMu 设备初始化。
3. ADB / minitouch 初始化。
4. 1280×720 屏幕捕获。
5. Our Notes 歌曲识别。
6. 难度选择。
7. Our Notes 谱面解析。
8. `237 notes` 的 tick → time 转换。
9. 演奏界面 `OurNotes_PlayChart` 的模板识别。

真正失败的位置发生在：

```text
Play Action
  -> play_song() 后台线程启动
  -> wait_first_note()
  -> current_player.resolution == None
  -> get_runtime_info(current_player.resolution, GAME_TYPE)
  -> resolution[0]
  -> TypeError: 'NoneType' object is not subscriptable
```

日志中的错误连续以约 100ms 周期重复，符合 `wait_first_note()` 异常处理后 `time.sleep(0.1)` 的行为。

因此，本次不是：

- Our Notes 谱面解析失败；
- 237 个 Note 数据损坏；
- OCR 识别导致的失败；
- MaaFramework 截图失败；
- minitouch 初始化失败；
- 结算逻辑导致演奏没有开始。

而是：

> **演奏阶段依赖的 `current_player.resolution` 返回了 `None`。同时 `Play.run()` 使用 daemon 后台线程，导致线程内部异常没有反馈给 MaaFramework，于是 MAA 把已经失败的 Play Action 错误地视为成功。**

---

## 2. 相关代码关系

重点检查以下代码路径：

```text
Play.run()
  └─ threading.Thread(target=play_song, daemon=True)
       └─ play_song()
            └─ wait_first_note()
                 └─ get_runtime_info(current_player.resolution, GAME_TYPE)
```

重点文件：

- `autodori.py`
- `player.py`
- `chart.py`
- `util.py`

日志：

- `autodori-20260930-014228.log`
- `maafw.log`

---

## 3. 证据链

### 3.1 屏幕分辨率本身是正常的

MAA 日志已经确认当前画面为：

```text
1280 × 720
```

因此不能把问题简单定义为“模拟器没有分辨率”。

真正异常的是自定义播放器对象的：

```python
current_player.resolution
```

在进入演奏阶段时返回了 `None`。

---

### 3.2 谱面解析正常

本次日志明确出现：

```text
_process_ournotes_time_chart: Succeed: 237 notes
```

这说明谱面已经进入时间化处理阶段。

因此本次修复不要修改：

- tick → time 转换；
- Note 排序；
- `notes_to_actions()`；
- `actions_to_MNTcmd()`；
- 谱面读取格式；

除非后续新日志证明这些部分存在独立问题。

---

### 3.3 演奏页面识别成功

MAA 成功识别：

```text
OurNotes_PlayChart
```

并且识别区域为：

```text
[0, 0, 1280, 720]
```

这说明程序确实进入了真正的演奏画面。

---

### 3.4 错误出现在 `wait_first_note()`

当前逻辑类似：

```python
def wait_first_note():
    ...
    info = get_runtime_info(current_player.resolution, GAME_TYPE)
    ...
```

而 `get_runtime_info()` 首先使用：

```python
x_zoom_multiple = resolution[0] / 1280
y_zoom_multiple = resolution[1] / 720
```

因此：

```python
resolution is None
```

即可直接解释本次错误：

```text
'NoneType' object is not subscriptable
```

---

## 4. 第一优先级修复：不要依赖 `player.resolution` 获取当前截图尺寸

### 目标

在 `wait_first_note()` 中使用实际截图对象的尺寸，而不是依赖：

```python
current_player.resolution
```

推荐从 `screen.shape` 获取：

```python
resolution = (screen.shape[1], screen.shape[0])
```

原因：当前流程已经证明截图链路能获得有效的 1280×720 图像，因此直接从实际帧获得分辨率比依赖播放器内部缓存属性可靠。

### 推荐实现原则

进入 `wait_first_note()` 后，应先获取一帧有效截图：

```python
screen = current_player.ipc_capture_display()
```

然后检查：

```python
if screen is None:
    ...
```

只有成功取得截图，才能进一步计算：

```python
resolution = (screen.shape[1], screen.shape[0])
```

再调用：

```python
info = get_runtime_info(resolution, GAME_TYPE)
```

### 推荐代码方向

不要机械复制以下代码，而是结合现有项目结构修改：

```python
def wait_first_note():
    last_color = None
    waited_frames = 0
    freezed = False
    consecutive_failures = 0

    screen = current_player.ipc_capture_display()
    if screen is None:
        raise RuntimeError("Failed to capture screen before waiting for first note")

    resolution = (screen.shape[1], screen.shape[0])
    info = get_runtime_info(resolution, GAME_TYPE)["wait_first"]
    from_row, to_row = info["from"], info["to"]

    while True:
        try:
            screen = current_player.ipc_capture_display()

            if screen is None:
                consecutive_failures += 1
                if consecutive_failures > 10:
                    raise RuntimeError(
                        "Failed to capture screen for 10 consecutive attempts"
                    )
                time.sleep(0.1)
                continue

            consecutive_failures = 0

            cur_color, _ = get_color_eval_in_range(
                screen,
                from_row,
                to_row,
            )

            # 保留现有首 Note 检测算法，不在本次修复中改变判定阈值。
            # ...

        except Exception as e:
            logging.error(
                f"Exception in wait_first_note: {e}",
                exc_info=True,
            )
            time.sleep(0.1)
```

### 重要

本次第一阶段修复只解决“分辨率来源不可靠”的问题，不要同时重写首 Note 检测算法，否则无法判断修复到底解决了哪个问题。

---

## 5. 第二优先级修复：让 Play 线程异常能够反馈给 MAA

当前代码存在一个结构性问题：

```python
play_thread = threading.Thread(target=play_song, daemon=True)
play_thread.start()
return CustomAction.RunResult(True)
```

这种实现意味着：

```text
线程启动成功 ≠ 演奏成功
```

MAA 只能知道线程被启动了，而不知道 `play_song()` 是否随后发生异常。

本次正是因此出现：

```text
Python：wait_first_note() 持续异常
MAA：Play Action = Succeeded
```

这是需要修复的。

### 推荐方案

保留异步运行方式也可以，但需要建立线程状态：

```python
play_error = None
play_finished = False
```

或者直接使用 `Future` / `queue.Queue` / 线程事件对象等可靠方式传递异常。

最少要求：

```python
try:
    play_song()
except Exception as e:
    logging.exception("play_song failed")
    把异常状态传回 Play.run()
```

不要出现：

```python
except Exception:
    pass
```

这种吞掉真正故障的处理。

### 行为目标

当 `play_song()` 初始化阶段失败时，MAA 不应该看到：

```text
ret=true
Node.Action.Succeeded
```

而应该能够进入失败路径或至少获得明确错误状态。

---

## 6. 第三优先级：增加诊断日志

下一次测试需要记录“同一个运行过程中分辨率到底什么时候发生变化”。

至少添加以下日志。

### 在歌曲数据准备阶段记录

```python
logging.debug(
    f"Player resolution before save_song: {current_player.resolution!r}"
)
```

### 在 `wait_first_note()` 开始时记录

```python
logging.debug(
    f"Player resolution before wait_first_note: {current_player.resolution!r}"
)
```

### 从截图获取尺寸后记录

```python
logging.debug(
    f"Captured screen shape: {screen.shape}, resolution={resolution}"
)
```

### 记录屏幕方向

保留当前已有的：

```text
SurfaceOrientation: 1
```

但不要把 orientation 直接等价成 resolution。

---

## 7. `player.py` 的后续风险点

当前 `ipc_capture_display()` 对 MuMu 的实现需要重点检查游戏包名/显示 ID 获取逻辑，当前实现中存在类似：

```python
ipc_get_display_id("com.bilibili.star.bili")
```

的固定包名依赖。

这不是本次日志中已经确认的故障，因为本次错误在 `wait_first_note()` 获取 resolution 时就已经发生，因此第一阶段不要把它误判为根因。

但是在修复 resolution 后，需要马上验证：

1. `ipc_capture_display()` 是否仍然能拿到正确游戏画面；
2. 获取的 display_id 是否对应当前游戏；
3. 返回图片尺寸是否稳定为 `1280×720`；
4. 横屏状态切换后是否仍然稳定。

如果这里存在问题，再单独修复。

---

## 8. 不要修改的部分

除非新测试明确暴露独立错误，本次不要主动修改以下逻辑：

### 谱面解析

```text
Our Notes chart parser
```

### Note 数量

本次已经成功得到：

```text
237 notes
```

### tick → time

本次已经成功完成。

### MNT 命令生成

需要先让程序真正通过 `wait_first_note()`，才能判断命令生成/发送是否存在问题。

### MAA OCR / TemplateMatch

本次歌曲识别、进入演奏页面识别均正常。

### minitouch 初始化

本次初始化成功，没有证据说明它导致了当前故障。

---

## 9. 建议的修改顺序

按以下顺序实现，不要一次性大重构：

```text
Step 1
↓
修改 wait_first_note()
使 resolution 来自 screen.shape
↓
Step 2
↓
增加 None screenshot 检查
↓
Step 3
↓
增加 resolution / screen.shape / orientation 日志
↓
Step 4
↓
给 play_song() 增加异常上报
↓
Step 5
↓
重新运行一首 Our Notes 歌曲
↓
Step 6
↓
确认是否进入 mnt.send()
↓
Step 7
↓
如果失败，再分析新的第一条异常
```

不要在 Step 1 尚未验证前同时改变 timing 算法。

---

## 10. 验收标准

### A. resolution 问题已经解决

日志不再出现：

```text
'NoneType' object is not subscriptable
```

特别是 `wait_first_note()` 中不能持续重复这个错误。

### B. 截图数据有效

应该能够看到类似：

```text
Captured screen shape: (720, 1280, ...)
resolution=(1280, 720)
```

具体 shape 的第三维是否存在取决于当前图像格式，不要硬编码维度。

### C. 首 Note 等待能够结束

应该看到类似：

```text
Picture changed: ...
The first note falls between ...
```

并继续执行后续演奏逻辑。

### D. 真正进入触控发送

日志中应该能够确认：

```text
mnt.send(...)
```

或现有项目中等价的触控发送日志出现。

本次原始运行不能达到这个阶段，因此这是验证修复是否真正推进流程的关键指标。

### E. MAA 不再出现假成功

不能再出现：

```text
Play Action
→ python 后台线程异常
→ MAA 仍然立即 ret=true
```

至少在演奏初始化失败时，MAA 必须得到可识别的失败状态，或者由外层状态机检测到 Play 线程失败。

---

## 11. 新一轮测试时重点观察日志

按照下面的顺序查看：

```text
1. Start play
2. Player resolution before wait_first_note
3. Captured screen shape
4. First-note waiting 状态变化
5. first note detected
6. command queue 开始执行
7. mnt.send
8. song end
9. settle overview
```

如果第 2~5 步正常，而第 6~7 步失败，那么问题已经从“resolution 初始化”转移到了触控命令执行层，应当开始检查 `command_queue`、MNT 指令内容、时间调度以及 `mnt.send()`。

如果第 3 步就拿不到截图，则优先检查 `player.py` 的 `ipc_capture_display()` 和 display_id，而不是继续修改 `wait_first_note()`。

如果第 5 步成功、第 6 步没有进入，检查 `wait_first_note()` 的退出逻辑。

---

## 12. 推荐的最小修改原则

Vibe Coding 工具执行时必须遵循以下约束：

> 先修复已被日志证明的问题，再处理可能存在的问题。

具体而言：

```text
必须修复：
current_player.resolution == None
        ↓
wait_first_note() 崩溃

必须修复：
Play 后台线程异常不能反馈给 MAA

建议补充：
截图 None 检查
resolution / screen.shape 日志
play_song 异常日志

暂不修改：
谱面解析
时间转换
Note 排序
触控 timing 算法
OCR
MAA TemplateMatch
```

禁止为了让程序“看起来继续运行”而简单写：

```python
resolution = current_player.resolution or (1280, 720)
```

然后不记录真实状态。

固定回退 `(1280, 720)` 可以作为临时诊断手段，但正式修复应优先使用实际截图的尺寸，以避免以后分辨率变化时产生隐藏错误。

同样禁止：

```python
except Exception:
    pass
```

或者通过无限重试掩盖根因。

---

## 13. 给 Vibe Coding Agent 的执行指令

请先阅读：

```text
autodori.py
player.py
chart.py
util.py
```

然后根据本文件修改代码。

执行时必须：

1. 找到 `wait_first_note()` 当前完整实现；
2. 找到 `get_runtime_info()` 对 resolution 的依赖；
3. 找到 `Player.resolution` 的实际来源；
4. 找到 `Play.run()` 和 `play_song()` 的线程关系；
5. 做最小范围修改；
6. 保留现有 Our Notes 谱面算法和 Note 时间算法；
7. 增加必要的诊断日志；
8. 不要用固定值掩盖 `None`；
9. 修改后进行静态检查/运行级检查，确保没有明显语法错误；
10. 输出修改的文件、修改原因和验证结果。

如果测试环境可以运行实际 MuMu + Our Notes，则至少完整跑通一次：

```text
进入歌曲
→ 加载谱面
→ 进入演奏界面
→ 首 Note 等待
→ 开始触控发送
```

如果无法运行实际游戏，则至少通过单元级/模拟方式验证：

```text
screen.shape -> resolution
resolution -> get_runtime_info()
Play 线程异常 -> 能被外层观察到
screen is None -> 能正确处理
```

---

## 14. 当前问题的最终定位

可以把本次故障简化成下面一句话：

> **Our Notes 自动打歌已经成功进入演奏页并完成 237 Note 谱面解析，但 `wait_first_note()` 使用的 `current_player.resolution` 在演奏阶段变成了 `None`，导致后台 `Play` 线程在真正发送触控前持续异常；同时 `Play.run()` 又把后台线程的“启动成功”错误当作“演奏成功”，从而让 MaaFramework 继续执行后续结算流程。**

因此第一修复点是 `wait_first_note()` 的分辨率获取方式，第二修复点是 Play 线程的异常传播机制。

本次日志没有证据证明谱面数据、MAA OCR、minitouch 或时间计算是导致本次失败的根因，因此不要将修改范围扩大到这些模块。
