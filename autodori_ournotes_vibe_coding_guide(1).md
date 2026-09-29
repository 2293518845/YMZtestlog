# AutoDori / Our Notes：MuMu v5 IPC 截图与首 Note 同步修复指南

> 用途：本文件用于 Vibe Coding / Agent Coding。
> 目标：修复当前 `Our Notes` 模式进入演奏后 `wait_first_note()` 无限报错的问题，并为下一阶段的首 Note 同步调试建立可观测性。
> 基于：`autodori(2).py`、`maafw.log`、`autodori-20260930-014228.log`，以及仓库中的实际 `autodori.py / player.py / util.py`。

## 1. 当前问题概述

本次运行已经完成了 MAA 初始化、MuMu v5 与 minitouch 初始化、Our Notes 选曲、歌曲 OCR、歌曲模糊匹配、谱面加载、237 个 Note 的解析以及设备方向获取，随后进入演奏阶段。

关键时间线：

```text
MAA 初始化
  ↓
MuMu v5 + minitouch 初始化
  ↓
Our Notes 选曲
  ↓
OCR 识别歌曲
  ↓
歌曲模糊匹配
  ↓
Our Notes 谱面解析：237 notes
  ↓
SurfaceOrientation = 1
  ↓
Save song
  ↓
进入演奏界面
  ↓
Play 自定义 Action 启动后台线程
  ↓
wait_first_note()
  ↓
'NoneType' object is not subscriptable
  ↓
约每 100 ms 重复一次
```

日志中最关键的一段是：

```text
01:42:38.328  Match result with default model: ('春日影 (MyGO!!!!! ver.)', 97)
01:42:38.536  _process_ournotes_time_chart: Succeed: 237 notes
01:42:38.605  SurfaceOrientation: 1
01:42:38.609  Save song: "春日影 (MyGO!!!!! ver.)"

01:42:52.819  Start play
01:42:52.820  Exception in wait_first_note: 'NoneType' object is not subscriptable
01:42:52.922  Exception in wait_first_note: 'NoneType' object is not subscriptable
01:42:53.026  Exception in wait_first_note: 'NoneType' object is not subscriptable
```

最后的 `maafw.log` 出现 `Close log`，结合本次是手动关闭程序，应视为正常收尾，而不是新的故障。

## 2. 已确认正常的部分

### 2.1 MAA 初始化正常

MAA Framework 正常启动，并且后续能够继续执行 OCR、TemplateMatch、Click 等操作，因此不是 MAA 整体初始化失败。

### 2.2 MuMu v5 与 minitouch 初始化正常

日志包含：

```text
device HBP-AL00 online
minitouch already installed in 127.0.0.1:16384
start minitouch: ...
Type B touch device Xiaomi Input (720x1280 with 32 contacts)
hard-limiting maximum number of contacts to 10
...
Mumu and MNT inited.
```

因此当前问题不要优先归因于 ADB、minitouch 或模拟器完全无法连接。

### 2.3 Our Notes 选曲和谱面解析正常

日志已经得到：

```text
Match result with default model: ('春日影 (MyGO!!!!! ver.)', 97)
_process_ournotes_time_chart: Succeed: 237 notes
SurfaceOrientation: 1
Save song: "春日影 (MyGO!!!!! ver.)"
```

因此当前问题不是歌曲识别、Song ID、谱面文件加载或 Note 解析首先失败。

不要为了修复本次问题而先修改 Chart 解析算法，除非后续日志明确证明 Chart 本身有问题。

## 3. 一级根因：Our Notes 使用了错误的 MuMu IPC 包名

### 3.1 当前代码

`player.py` 的 MuMu 分支目前类似：

```python
if self.type.startswith("mumu"):
    if self.display_id == -1:
        self.display_id = self.player.ipc_get_display_id(
            "com.bilibili.star.bili"
        )

    return self.player.ipc_capture_display(self.display_id)[:, :, :3]
```

其中 `com.bilibili.star.bili` 的代码注释明确把它作为旧 BanG Dream B服包名使用。

### 3.2 Our Notes 的实际包名

当前 Our Notes Android 包名为：

```text
com.bilibili.sirius
```

因此 Our Notes 模式不能继续固定查询：

```text
com.bilibili.star.bili
```

应该使用：

```text
com.bilibili.sirius
```

### 3.3 为什么这会导致当前异常

当前调用链很可能是：

```text
player.py
  ↓
ipc_get_display_id("com.bilibili.star.bili")
  ↓
得到错误 / 无效的目标 display，或无法得到有效 display
  ↓
ipc_capture_display(display_id)
  ↓
底层截图失败
  ↓
返回 None
  ↓
None[:, :, :3]
  ↓
TypeError: 'NoneType' object is not subscriptable
```

这与日志中 `wait_first_note()` 反复出现的异常高度吻合。

## 4. 为什么当前错误文本与 player.py 高度吻合

`player.py` 直接做：

```python
frame = self.player.ipc_capture_display(self.display_id)
return frame[:, :, :3]
```

当 `frame is None` 时，Python 会产生：

```text
TypeError: 'NoneType' object is not subscriptable
```

本次日志恰好反复出现完全相同的异常，因此这是当前最强的根因假设。

不过当前 `wait_first_note()` 只记录异常字符串，没有 traceback，因此最终验证时必须增加 `logging.exception()`，让下一次运行直接指出具体源码行。

## 5. 必须实施的代码修改

### 5.1 不要继续硬编码单一游戏包名

推荐将 `package_name` 作为 `Player` 的参数，而不是让 `player.py` 知道 `GAME_TYPE`。

建议结构：

```python
class Player:
    def __init__(self, type_, path, index, package_name=None):
        self.type = type_
        self.package_name = package_name
        self.display_id = -1

        if type_ == "mumuv4":
            self.player = mumuipc.MuMuPlayer(path, index, "v4")
        elif type_ == "mumuv5":
            self.player = mumuipc.MuMuPlayer(path, index, "v5")
        elif type_ == "ld":
            self.player = ldipc.LDPlayer(path, index)
```

MuMu 截图建议改成：

```python
def ipc_capture_display(self):
    if self.type.startswith("mumu"):
        if self.display_id == -1:
            if not self.package_name:
                raise RuntimeError("MuMu IPC capture requires package_name")

            self.display_id = self.player.ipc_get_display_id(
                self.package_name
            )

            if self.display_id is None or self.display_id < 0:
                raise RuntimeError(
                    f"Failed to get display_id: "
                    f"package={self.package_name}, "
                    f"display_id={self.display_id}"
                )

        frame = self.player.ipc_capture_display(self.display_id)

        if frame is None:
            raise RuntimeError(
                f"MuMu IPC screenshot returned None: "
                f"package={self.package_name}, "
                f"display_id={self.display_id}"
            )

        return frame[:, :, :3]

    return self.player.capture()
```

### 5.2 `autodori.py` 初始化 Player 时按游戏选择包名

不要让 `player.py` 自己猜 `GAME_TYPE`。

推荐：

```python
package_name = {
    "bangdream": "com.bilibili.star.bili",
    "ournotes": "com.bilibili.sirius",
}[GAME_TYPE]

current_player = player.Player(
    type_,
    Path(path),
    index,
    package_name=package_name,
)
```

这样以后新增游戏时，只需要扩展映射，不需要修改 IPC 核心逻辑。

## 6. 必须加强异常日志

当前 `wait_first_note()` 类似：

```python
except Exception as e:
    logging.error(f"Exception in wait_first_note: {e}")
    time.sleep(0.1)
```

这会把 traceback 吞掉。

建议改成：

```python
except Exception:
    consecutive_failures += 1

    logging.exception(
        "Exception in wait_first_note "
        f"(consecutive_failures={consecutive_failures})"
    )

    if consecutive_failures >= 10:
        logging.error(
            "Failed to get a valid frame 10 times, "
            "aborting wait_first_note."
        )
        raise

    time.sleep(0.1)
```

### 6.1 为什么原来的 10 次保护无效

原逻辑只在：

```python
if screen is None:
    consecutive_failures += 1
```

这个分支里增加失败次数。

但是如果 `ipc_capture_display()` 内部已经因为 `None[:, :, :3]` 抛异常，代码会直接跳进外层 `except`，根本到不了 `if screen is None`，所以 `consecutive_failures` 永远不会增加。

必须把“异常”和“返回 None”都纳入失败计数。

### 6.2 成功获取有效 frame 后重置失败计数

```python
screen = current_player.ipc_capture_display()

if screen is None:
    consecutive_failures += 1
    ...
else:
    consecutive_failures = 0
```

这样连续失败计数才真正表示当前截图链路的健康状态。

## 7. 建议增加 IPC 初始化诊断

第一次取得 display ID 后记录：

```python
logging.debug(
    f"MuMu IPC target: "
    f"type={self.type}, "
    f"package={self.package_name}, "
    f"display_id={self.display_id}, "
    f"resolution={self.player.resolution}"
)
```

第一次真正截图后记录：

```python
logging.debug(
    f"MuMu IPC frame: "
    f"type={type(frame).__name__}, "
    f"shape={getattr(frame, 'shape', None)}, "
    f"dtype={getattr(frame, 'dtype', None)}"
)
```

下一次运行至少必须能回答：

```text
当前目标包名是什么？
display_id 是多少？
截图返回 None 还是 ndarray？
如果是 ndarray，它的 shape / dtype 是什么？
```

不要高频打印完整 ndarray 或完整截图内容，避免调试日志本身造成性能问题。

## 8. 第二个潜在问题：Our Notes 首 Note 检测区域仍是占位配置

这个问题不是本次 `NoneType` 异常的直接根因，但 IPC 修复以后必须检查。

`wait_first_note()` 会调用：

```python
info = get_runtime_info(
    current_player.resolution,
    GAME_TYPE
)["wait_first"]

from_row, to_row = info["from"], info["to"]
```

仓库中的 Our Notes `wait_first` 区域仍属于占位配置。因此后续可能出现：

```text
IPC 截图修复
  ↓
wait_first_note() 不再抛异常
  ↓
成功获取画面
  ↓
但 from/to 没有覆盖第一个 Note 的实际路径
  ↓
颜色变化始终达不到阈值
  ↓
wait_first_note() 一直等待
```

不要把“IPC 失败”和“同步区域不正确”混为一个问题。

正确顺序应该是：

```text
1. 确认 IPC frame 有效
2. 确认 wait_first 区域覆盖实际判定路径
3. 确认 Picture freezed 逻辑正确
4. 确认颜色变化阈值正确
5. 确认首 Note 时间基准正确
6. 最后才分析 MNT 时序
```

## 9. `wait_first_note()` 的调试信息建议

进入函数时增加：

```python
logging.debug(
    f"wait_first_note init: "
    f"resolution={current_player.resolution}, "
    f"from_row={from_row}, "
    f"to_row={to_row}"
)
```

颜色变化时记录关键数值：

```python
logging.debug(
    f"Picture changed: {change_score}, "
    f"cur_color={cur_color}, "
    f"last_color={last_color}"
)
```

进入冻结状态时：

```python
logging.debug("Picture freezed, waiting for the first note...")
```

检测到首 Note 时：

```python
logging.info(
    f"First note detected between rows {from_row}-{to_row}"
)
```

这些信息足够定位同步问题，不需要把完整截图写入日志。

## 10. `Play` 后台线程的架构问题

当前 `Play` 自定义 Action 是：

```python
play_thread = threading.Thread(
    target=play_song,
    daemon=True
)
play_thread.start()
return CustomAction.RunResult(True)
```

这里 `Play` 返回 success 只说明后台线程创建成功，并不代表 `play_song()` 已经成功开始演奏。

因此 MAA 会继续进入后续节点，而 Python 线程可能仍然卡在 `wait_first_note()`。

这正是本次日志所体现的现象：MAA 已经认为 `OurNotes_PlayChart` 的 Custom Action 成功，并继续尝试识别 `OurNotes_SettleOverview`，与此同时 Python 线程仍在重复报告 `wait_first_note()` 异常。

### 10.1 当前阶段不要立即大规模重构线程

先保证后台播放线程有明确状态，例如：

```python
play_state = {
    "status": "idle",
    "error": None,
}
```

推荐状态：

```text
idle
starting
waiting_first_note
playing
finished
failed
stopped
```

`play_song()` 至少应在状态变化时更新它：

```python
play_state["status"] = "waiting_first_note"

try:
    wait_first_note()
    play_state["status"] = "playing"
    ...
except Exception as e:
    play_state["status"] = "failed"
    play_state["error"] = repr(e)
    raise
```

这项重构放在 IPC 修复验证之后即可。

## 11. 当前阶段不要修改的部分

### 11.1 不要先修改 Chart 时间转换

本次日志已经证明：

```text
_process_ournotes_time_chart: Succeed: 237 notes
```

因此第一优先级是截图同步链。

### 11.2 不要先修改 MNT 坐标

当前还没有证据证明 `androidxy_to_MNTxy`、orientation 或 MNT 尺寸转换有问题。

### 11.3 不要先修改 `PHOTOGATE_LATENCY`

当前 `wait_first_note()` 根本没有成功结束，30 ms 光电门补偿尚未真正进入首拍触控执行阶段。

### 11.4 不要先修改防累计误差算法

当前版本使用绝对时间：

```python
current_time_s = time.perf_counter() - start_time
delta = target_time_s - current_time_s
```

由于本次尚未进入命令发送循环，目前无法从本次日志判断累计误差问题。

## 12. 修复后的验收标准

### 阶段 A：IPC

运行后至少应看到类似：

```text
MuMu IPC target: type=mumuv5
MuMu IPC target: package=com.bilibili.sirius
display_id=<有效数字>
MuMu IPC frame: type=ndarray
MuMu IPC frame: shape=(..., ..., 4)
```

并且不再出现：

```text
'NoneType' object is not subscriptable
```

### 阶段 B：首 Note 同步

应能出现：

```text
Start play
Picture freezed, waiting for the first note...
First note detected between rows ...
```

随后才进入真正的播放命令调度。

### 阶段 C：触控

只有看到实际 MNT 命令发送以及回调日志后，才开始分析：

- Note 时间误差
- MNT 命令执行耗时
- command interval
- wait / move / up / down cost
- 首拍偏移
- 累计误差
- 滑条切片
- 多指同步

### 阶段 D：MAA

MAA 不应继续把“后台线程创建成功”当作“歌曲播放成功”。后续应能够区分：

```text
playing
finished
failed
stopped
```

但这不是当前 IPC 修复的第一步。

## 13. Agent 执行顺序

请 Agent 严格按顺序执行，避免一次改动过多导致无法定位问题：

```text
1. 检查 player.py
   ↓
2. 增加 package_name 参数
   ↓
3. Our Notes 使用 com.bilibili.sirius
   ↓
4. 对 display_id 做合法性检查
   ↓
5. 对 frame=None 做显式检查
   ↓
6. wait_first_note() 使用 logging.exception()
   ↓
7. 异常路径纳入 consecutive_failures
   ↓
8. 运行验证
   ↓
9. 若 IPC 正常，再检查 wait_first 坐标
   ↓
10. 再检查首 Note 时间同步
   ↓
11. 最后才进入 MNT 精度/累计误差分析
```

不要把以上步骤合并成一次“大重构”。

## 14. 最小验证补丁

如果 Agent 需要先做最小修改，可以先用以下方式验证根因，而暂时不重构完整 Player API。

### `player.py`

```python
def ipc_capture_display(self):
    if self.type.startswith("mumu"):
        if self.display_id == -1:
            package_name = "com.bilibili.sirius"
            self.display_id = self.player.ipc_get_display_id(package_name)

            if self.display_id is None or self.display_id < 0:
                raise RuntimeError(
                    f"Failed to get display id for {package_name}: "
                    f"{self.display_id}"
                )

        frame = self.player.ipc_capture_display(self.display_id)

        if frame is None:
            raise RuntimeError(
                f"MuMu IPC returned None: display_id={self.display_id}"
            )

        return frame[:, :, :3]

    return self.player.capture()
```

### `autodori.py`

```python
except Exception:
    consecutive_failures += 1
    logging.exception(
        "Exception in wait_first_note: "
        f"consecutive_failures={consecutive_failures}"
    )

    if consecutive_failures >= 10:
        logging.error("wait_first_note aborted after 10 failures")
        raise

    time.sleep(0.1)
```

这个最小补丁的目的只是快速验证：

```text
旧包名 → com.bilibili.sirius
```

是否就是当前 `NoneType` 的实际原因。

## 15. 最终目标架构

```text
GAME_TYPE
   │
   ├── bangdream
   │      └── com.bilibili.star.bili
   │
   └── ournotes
          └── com.bilibili.sirius
                    │
                    ↓
              Player.package_name
                    │
                    ↓
             ipc_get_display_id()
                    │
                    ↓
              valid display_id
                    │
                    ↓
          ipc_capture_display()
                    │
             ┌──────┴──────┐
             │             │
           None         ndarray
             │             │
          raise          RGB
          error            │
                           ↓
                  wait_first_note()
                           │
                           ↓
                  first Note detection
                           │
                           ↓
                      start_time
                           │
                           ↓
                 absolute-time playback
                           │
                           ↓
                         MNT
```

核心原则：游戏选择、应用包名、display 获取和截图验证应该成为一条明确的数据链，而不是让 `player.py` 隐式假设当前游戏。

## 16. Agent 完成任务后的汇报格式

完成修改后，不要只回复“已修复”。至少汇报：

```text
1. 修改了哪些文件
2. 每个文件修改了什么
3. Our Notes 使用的 package_name
4. display_id 是否成功获得
5. IPC capture 返回类型和 shape
6. wait_first_note 是否成功结束
7. 是否开始发送 MNT 命令
8. 如果失败，完整 traceback
9. 下一步需要检查什么
```

其中第 5、6、7 项最重要，因为它们决定下一轮调试应该进入哪一层。

## 17. 当前结论

当前最有证据支持的故障链是：

```text
Our Notes
  ↓
player.py 仍使用旧 Bang Dream 包名
  ↓
MuMu IPC display 获取/截图链路异常
  ↓
ipc_capture_display() 返回 None
  ↓
[:, :, :3]
  ↓
'NoneType' object is not subscriptable
  ↓
wait_first_note() 捕获异常并约每 100ms 重试
  ↓
异常路径没有正确增加失败计数，因此形成无限循环
```

优先修复：

```text
com.bilibili.star.bili
        ↓
com.bilibili.sirius
```

同时增加：

```text
frame None 检查
display_id 检查
logging.exception()
异常路径失败计数
```

修复后再验证 `wait_first` 区域和首 Note 同步。

不要把本次问题误判为谱面解析问题、MNT 时间问题或累计误差问题；当前日志尚未进入这些阶段。

## 18. 证据定位

当前对话中已经验证的关键证据：

- `autodori(2).py`：`save_song()`、`play_song()`、`wait_first_note()`、`Play` 自定义 Action。
- `maafw.log`：MAA 初始化、Our Notes 选曲流程、`OurNotes_PlayChart` Custom Action 成功、随后结算识别失败，以及最后的 `Close log`。
- `autodori-20260930-014228.log`：歌曲识别 97 分、237 notes、Orientation=1、`Start play` 后持续 `NoneType` 异常。
- 仓库实际 `player.py`：MuMu v5 通过硬编码 `com.bilibili.star.bili` 获取 display，并直接执行 `frame[:, :, :3]`。
- 仓库实际 `util.py`：Our Notes `wait_first` 仍为占位配置，需要在 IPC 修复后单独验证。

本文件的目的不是重写整个自动打歌系统，而是把当前故障限定在正确的层级，并让 Agent 按可验证的步骤逐层推进。
