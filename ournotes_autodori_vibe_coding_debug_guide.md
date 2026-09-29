# Our Notes / autodori `wait_first_note()` 故障修复与 Vibe Coding 指导

## 1. 任务目标

本任务针对 2026-09-30 的一次实际运行故障进行修复。现象是：

- 歌曲 OCR 成功；
- Our Notes 谱面解析成功，得到 237 个 Note；
- MuMu 与 minitouch 初始化成功；
- 开始打歌后，`wait_first_note()` 持续出现：

```text
Exception in wait_first_note: 'NoneType' object is not subscriptable
```

- 异常约每 100 ms 重复一次；
- 日志最后停止是用户手动关闭程序造成的，不应把日志末尾当作程序自然退出；
- MAA 同时认为 `OurNotes_PlayChart` Custom Action 成功，并继续执行后续结算识别。

目标不是简单“压掉异常”，而是把截图链路、异常处理和 MAA Custom Action 生命周期修正确，使实际打歌线程的状态能够被可靠感知。

---

## 2. 已确认的执行链

当前代码的关键执行关系：

```text
MAA Pipeline
    │
    ├─ OurNotes_WaitLoading
    │       ↓
    │    成功
    │       ↓
    ├─ OurNotes_PlayChart
    │       ↓
    │    Custom Action 启动 play_song daemon thread
    │       ↓
    │    Custom Action 立即返回 success=True
    │       ↓
    │    MAA 继续执行后续节点
    │
    └─ OurNotes_SettleOverview

Python 后台线程
    │
    ├─ play_song()
    │
    ├─ wait_first_note()
    │
    └─ current_player.ipc_capture_display()
             ↓
       Player.ipc_capture_display()
             ↓
       MuMu IPC ipc_capture_display(display_id)
             ↓
       返回 None
             ↓
       对 None 执行 [:, :, :3]
             ↓
       TypeError:
       'NoneType' object is not subscriptable
             ↓
       wait_first_note() 捕获异常
             ↓
       sleep(0.1)
             ↓
       无限重复
```

---

## 3. 当前最重要的根因

### 3.1 `player.py` 的截图函数存在防御缺口

当前逻辑类似：

```python
frame = self.player.ipc_capture_display(self.display_id)
return frame[:, :, :3]
```

如果底层 MuMu IPC 返回：

```python
None
```

那么：

```python
None[:, :, :3]
```

会直接产生：

```text
'NoneType' object is not subscriptable
```

因此上层 `autodori.py` 中类似：

```python
screen = current_player.ipc_capture_display()

if screen is None:
    ...
```

实际上无法处理这个情况。

原因是 `None` 在 `player.py` 内部已经被切片操作触发异常，根本不会作为返回值传到 `autodori.py`。

---

## 4. 第二个高可疑点：MuMu Display ID

`player.py` 当前针对 MuMu 的逻辑使用：

```python
self.player.ipc_get_display_id(
    "com.bilibili.star.bili"
)
```

这是原有 BanG Dream / 其他游戏逻辑留下的硬编码目标。

当前项目已经在运行 Our Notes，因此必须确认：

1. `com.bilibili.star.bili` 是否仍然是当前目标游戏；
2. Our Notes 当前运行的包名是什么；
3. `ipc_get_display_id()` 返回值是否有效；
4. 返回的 Display ID 是否属于当前游戏；
5. `ipc_capture_display(display_id)` 在该 Display ID 上是否能够稳定返回图像。

不要在没有验证之前直接认定“包名一定错误”。它目前是最高优先级怀疑点，但需要运行时日志确认。

---

## 5. 第一阶段修改：让截图失败可观察、可恢复

修改 `Player.ipc_capture_display()`，不要直接对 IPC 返回值切片。

推荐结构：

```python
def ipc_capture_display(self):
    if self.type.startswith("mumu"):
        if self.display_id == -1:
            self.display_id = self.player.ipc_get_display_id(
                TARGET_PACKAGE_NAME
            )
            logging.info(
                f"MuMu display_id = {self.display_id}"
            )

        frame = self.player.ipc_capture_display(self.display_id)

        logging.info(
            "MuMu IPC capture: "
            f"type={type(frame)}, "
            f"is_none={frame is None}, "
            f"shape={getattr(frame, 'shape', None)}"
        )

        if frame is None:
            return None

        return frame[:, :, :3]
```

要求：

- `TARGET_PACKAGE_NAME` 不要继续散落硬编码；
- 包名应集中配置；
- 不要通过 `try/except` 静默吞掉截图失败；
- `frame is None` 时必须让上层知道；
- 正常帧仍然保持原来的 RGB 三通道输出。

---

## 6. 第二阶段修改：修复 `wait_first_note()` 的异常处理

当前类似：

```python
except Exception as e:
    logging.error(f"Exception in wait_first_note: {e}")
    time.sleep(0.1)
```

这会丢失 traceback，导致无法知道到底是哪一行出错。

至少改成：

```python
except Exception:
    logging.exception("Exception in wait_first_note")
    time.sleep(0.1)
```

但不要只停留在打印 traceback。

建议进一步区分：

```text
截图返回 None
    ↓
计数
    ↓
连续失败达到阈值
    ↓
明确结束本次播放
```

与：

```text
程序 Bug / TypeError / ValueError
    ↓
记录完整 traceback
    ↓
不要无限重试
```

尤其不要让任意 Python 异常都变成永久 100 ms 重试。

---

## 7. 第三阶段修改：让 `consecutive_failures` 真正有效

当前代码已经存在类似：

```python
if screen is None:
    consecutive_failures += 1

    if consecutive_failures >= 10:
        ...
```

但由于 `player.py` 在 `None` 上提前执行切片，这段代码以前无法处理 MuMu IPC 返回 `None` 的情况。

修复 `player.py` 后，这套逻辑才能真正工作。

建议行为：

```text
capture -> None
    ↓
consecutive_failures += 1
    ↓
短暂等待
    ↓
再次 capture
    ↓
连续失败 < MAX_CAPTURE_FAILURES
    → 继续等待

连续失败 >= MAX_CAPTURE_FAILURES
    → 记录 display_id / package / frame 状态
    → 终止 wait_first_note
    → 将播放状态标记为失败
```

不要无限重试。

---

## 8. 第四阶段：检查 `get_runtime_info()` 和 Our Notes ROI

当前源码中 Our Notes 的运行时区域仍有明显的适配性质/占位性质，例如：

```text
wait_first
from = 约 400
to   = 约 450
```

以及：

```text
判定线 Y ≈ 576
```

这些坐标必须在截图链路修复之后再验证。

注意：

**当前 `NoneType` 的第一故障不能用 ROI 错误解释。**

因为当前错误发生在：

```text
IPC frame
    ↓
frame[:, :, :3]
```

而不是：

```text
get_color_eval_in_range()
```

所以不要先大规模修改 Note 坐标、颜色阈值或轨道算法。

正确顺序是：

```text
1. 确认截图能拿到
2. 确认截图尺寸
3. 确认截图方向
4. 确认 ROI
5. 确认颜色分析
6. 确认 first note 检测
7. 确认 Note → touch 坐标
8. 确认时间轴
```

---

## 9. 第五阶段：验证截图本身

修复后必须在日志中看到类似：

```text
MuMu display_id = <有效ID>
MuMu IPC capture:
    type=<numpy类型>,
    is_none=False,
    shape=(H, W, C)
```

对于当前 minitouch 初始化所见的 720×1280 环境，应重点确认截图的实际：

```text
height
width
channels
```

不要仅凭 minitouch 的：

```text
720 x 1280
```

推断 IPC 截图尺寸。

截图尺寸和触控坐标系必须分别验证。

---

## 10. Display ID 的验证任务

Vibe Coding 时优先增加诊断，而不是猜。

需要记录：

```python
logging.info(f"target package = {TARGET_PACKAGE_NAME}")
logging.info(f"display_id = {self.display_id}")
```

如果可以访问 MuMu IPC 的相关接口，应继续记录：

```text
当前 Activity
当前 package
display_id
capture result
capture shape
```

需要回答：

```text
A. 当前 Our Notes 的实际包名是什么？
B. TARGET_PACKAGE_NAME 是否正确？
C. ipc_get_display_id(package) 返回什么？
D. ipc_capture_display(display_id) 为什么返回 None？
```

只有 D 确认后，才能进一步决定是：

- 包名错误；
- Display ID 错误；
- 当前 Activity/Display 不支持；
- MuMu IPC 时机问题；
- 游戏切屏/加载期间暂时无帧；
- IPC API 使用方式错误。

---

## 11. MAA Custom Action 生命周期必须修复

当前最危险的架构问题之一：

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
MAA:
    “Custom Action 成功”

实际上:
    play_song() 可能刚刚启动
    或已经异常
    或正在无限重试
```

所以：

```text
RunResult(True)
```

不能代表实际打歌成功。

---

## 12. 推荐的播放线程状态设计

不要让 MAA 和后台线程完全脱钩。

至少建立明确状态：

```text
IDLE
STARTING
WAITING_FIRST_NOTE
PLAYING
SUCCEEDED
FAILED
CANCELLED
```

例如：

```python
play_state = {
    "status": "IDLE",
    "error": None,
}
```

播放线程：

```text
STARTING
    ↓
WAITING_FIRST_NOTE
    ↓
截图失败
    ↓
FAILED
```

或者：

```text
WAITING_FIRST_NOTE
    ↓
检测到第一 Note
    ↓
PLAYING
    ↓
command_queue 完成
    ↓
SUCCEEDED
```

这样后续 MAA Custom Action 才有机会根据实际状态返回结果。

---

## 13. 不要让 MAA 过早进入结算识别

当前实际时间线表现为：

```text
01:42:52.819
Start play

01:42:52.820
wait_first_note() 异常

01:42:52.9xx
继续异常

01:42:53.0xx
MAA 已经认为 PlayChart 成功

01:42:53.0xx
开始 OurNotes_SettleOverview

同时 Python:
wait_first_note()
仍然异常
```

这会造成：

```text
打歌线程尚未开始真正操作
        +
MAA 已进入结算检测
```

因此最终应让：

```text
OurNotes_PlayChart
```

的成功条件与：

```text
play_song()
```

实际生命周期建立关联。

---

## 14. minitouch 当前不是第一故障点

日志显示 minitouch 已成功建立：

```text
v 1
^ 10 720 1280 0
$ 10794
```

当前错误发生在第一 Note 等待阶段，尚未进入真正的：

```python
mnt.send(...)
```

因此第一阶段不要修改 minitouch。

只有在截图和 first-note 检测恢复后，如果出现：

```text
send failed
touch coordinate invalid
```

再单独排查触控层。

---

## 15. `get_color_eval_in_range()` 的定位结论

当前仓库的 `util.py` 中颜色分析函数正常路径会创建：

```python
avg_color
std_color
```

并返回类似：

```python
return avg_color, std_color
```

因此当前日志中的：

```text
'NoneType' object is not subscriptable
```

更符合：

```python
frame[:, :, :3]
```

而不是：

```python
cur_color[0:3]
```

这点不要在后续开发中重新误判。

---

## 16. 推荐修改顺序

严格按照以下顺序进行，避免一次改太多：

### Phase 1：诊断

只增加日志，不改变核心行为：

```text
target package
display_id
IPC capture type
IPC capture None 状态
IPC capture shape
```

同时：

```python
logging.exception(...)
```

输出 traceback。

### Phase 2：修复截图 None

把：

```python
return frame[:, :, :3]
```

改成安全处理：

```python
if frame is None:
    return None

return frame[:, :, :3]
```

### Phase 3：修复重试

把无限异常重试改成：

```text
有限次数
    ↓
失败
    ↓
明确返回失败状态
```

### Phase 4：确认 Our Notes 包名

确认：

```text
TARGET_PACKAGE_NAME
```

不要继续默认使用：

```text
com.bilibili.star.bili
```

### Phase 5：确认截图

确认：

```text
shape
orientation
内容是否真的是 Our Notes 游戏画面
```

### Phase 6：修复 ROI

再调整：

```text
wait_first
note area
判定线
轨道区域
```

### Phase 7：验证 first-note

必须看到：

```text
Start play
    ↓
capture OK
    ↓
color analysis OK
    ↓
first note detected
    ↓
start_time established
```

### Phase 8：验证实际触控

确认：

```text
mnt.send()
```

确实被调用，并且坐标正确。

### Phase 9：修复 MAA 生命周期

让：

```text
OurNotes_PlayChart
```

真正反映：

```text
play_song()
```

的成功/失败。

---

## 17. 不要采用的“修复”

不要：

```python
except Exception:
    pass
```

不要：

```python
return np.zeros(...)
```

来伪造截图成功。

不要把：

```python
None
```

强制转换成空数组后继续播放。

不要无限：

```python
while True:
    try:
        ...
    except:
        sleep(0.1)
```

不要在尚未证明截图正常之前大量调整：

```text
BPM
Note 时间
触摸延迟
PHOTOGATE_LATENCY
轨道坐标
```

否则会把基础设施故障和谱面算法故障混在一起。

---

## 18. Vibe Coding 验收标准

完成第一阶段后，一次测试运行至少应该能回答：

```text
[ ] 当前目标 package 是什么？
[ ] display_id 是什么？
[ ] display_id 是否有效？
[ ] IPC capture 是否返回 None？
[ ] 如果非 None，shape 是多少？
[ ] 截图内容是不是 Our Notes？
[ ] SurfaceOrientation 是多少？
[ ] wait_first ROI 是多少？
[ ] get_color_eval_in_range 是否成功？
[ ] 是否检测到 first note？
[ ] 是否进入 mnt.send()？
[ ] play_song 最终状态是什么？
[ ] MAA 是否在 play_song 完成前错误地进入 settle？
```

如果这些问题都能从日志中回答，后续调试就不再依赖猜测。

---

## 19. 推荐最终日志格式

建议最终形成类似：

```text
[PLAY] target_package=com.xxx.ournotes
[PLAY] display_id=123
[CAPTURE] success=True shape=(1280,720,4)
[CAPTURE] rgb_shape=(1280,720,3)
[PLAY] orientation=1
[PLAY] wait_first_roi=(400,450)
[PLAY] color_eval avg=[...]
[PLAY] first_note_detected=True
[PLAY] start_time=...
[PLAY] command_count=237
[PLAY] touch_send started
[PLAY] playback completed
[PLAY] status=SUCCEEDED
```

失败时则应该类似：

```text
[CAPTURE] success=False
[CAPTURE] display_id=123
[CAPTURE] consecutive_failures=10
[PLAY] status=FAILED
[PLAY] error=MuMu IPC capture returned None
```

而不是：

```text
Exception in wait_first_note: ...
Exception in wait_first_note: ...
Exception in wait_first_note: ...
```

---

## 20. 最终目标

本次开发不是单纯让程序“不报错”。

最终目标是建立一条可诊断、可恢复的 Our Notes 自动打歌链：

```text
Our Notes 启动
    ↓
确认目标进程
    ↓
获取正确 Display ID
    ↓
IPC 稳定截图
    ↓
确认截图尺寸/方向
    ↓
正确 ROI
    ↓
检测第一 Note
    ↓
建立准确 start_time
    ↓
执行 237 个 Note 的触控序列
    ↓
播放线程正常结束
    ↓
向 MAA 返回真实成功/失败
    ↓
MAA 再进入结算识别
```

当前最优先的工作只有两个：

1. 修复 `player.py` 中对 IPC `None` 直接切片的问题，并记录 `display_id` / capture 状态。
2. 查明 `com.bilibili.star.bili` 是否仍是当前 Our Notes 的正确目标包名，并验证对应 Display ID。

完成这两步之后，再继续处理 Our Notes 的 ROI、first-note 检测、触控坐标和时间同步。
