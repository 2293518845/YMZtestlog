import json
import logging
import time
from pathlib import Path

from minitouchpy import CommandBuilder
from peewee import *
from playhouse.sqlite_ext import JSONField

import util
from api import BestdoriAPI
import yaml


class PlayRecord(Model):
    """
    打歌记录数据模型，使用 peewee ORM 映射到 SQLite 数据库。
    """
    class Meta:
        database = SqliteDatabase("data/play_records.db")

    play_time = TimestampField() # 打歌时间
    play_offset = JSONField() # 打歌时的延迟补偿数据
    chart_id = CharField() # 谱面ID
    difficulty = CharField() # 难度
    succeed = BooleanField() # 是否成功完成
    result = JSONField() # 结算成绩（Perfect, Great 等）


PlayRecord.create_table(safe=True)


class Chart:
    """
    谱面处理类
    负责获取谱面数据，将基于 Beat 的谱面转换为基于 Time 的事件，
    并最终生成 minitouch 可执行的触控指令。
    """
    def __init__(self, id_and_difficulty: tuple[str, str] = None, song_name=None, game_type="bangdream"):
        self._id_, self._difficulty = id_and_difficulty
        self._song_name = song_name
        self._chart_name = f"{self._id_}-{self._difficulty}"
        self.game_type = game_type
        
        if self.game_type == "ournotes":
            # 从本地 JSON 文件加载 our notes 谱面
            self._chart_data = self._load_ournotes_chart()
        else:
            # 从 API 获取 bangdream 原始谱面数据
            self._chart_data = BestdoriAPI.get_chart(self._id_, self._difficulty)
            
        self._logger = logging.getLogger(self._chart_name)
        self._bpms = [] # 存储 BPM 变化信息
        self.actions = [] # 存储转换后的手指动作
        self._commands = [] # 存储最终的 minitouch 命令
        
        if self.game_type == "ournotes":
            self._total = len(self._chart_data.get("score", {}).get("notes", []))
            self._process_ournotes_time_chart()
        else:
            self._total = len(self._chart_data)
            # 初始化时处理谱面时间
            self._process_time_chart()

    def _load_ournotes_chart(self):
        """加载 our notes 的本地 JSON 谱面"""
        try:
            # 1. 读取 songs.json 获取 chart_key
            songs_path = Path("moenote/index/songs.json")
            if not songs_path.exists():
                raise FileNotFoundError(f"songs.json not found at {songs_path}")
                
            with open(songs_path, "r", encoding="utf-8") as f:
                songs_data = json.load(f)
                
            song_info = songs_data.get(str(self._id_))
            if not song_info:
                raise ValueError(f"Song ID {self._id_} not found in songs.json")
                
            # 难度映射: easy->EZ, normal->NM, hard->HD, expert->EX
            diff_map = {"easy": "EZ", "normal": "NM", "hard": "HD", "expert": "EX"}
            mapped_diff = diff_map.get(self._difficulty, "EX")
            
            diff_info = song_info.get("difficulties", {}).get(mapped_diff)
            if not diff_info:
                raise ValueError(f"Difficulty {mapped_diff} not found for song {self._id_}")
                
            chart_key = diff_info.get("chart_key")
            if not chart_key:
                raise ValueError(f"chart_key not found for song {self._id_} difficulty {mapped_diff}")
                
            # 2. 构建谱面文件路径并加载
            # chart_key 格式如 "0001/0001_00"
            chart_filename = f"{chart_key.split('/')[-1]}.json"
            chart_path = Path(f"moenote/storage/Live/MusicScore/{chart_key}/{chart_filename}")
            
            if not chart_path.exists():
                raise FileNotFoundError(f"Chart file not found at {chart_path}")
                
            with open(chart_path, "r", encoding="utf-8") as f:
                return json.load(f)
                
        except Exception as e:
            logging.error(f"Failed to load ournotes chart: {e}")
            return {"score": {"notes": [], "events": {"bpm": []}}}


    def _beat_to_time(self, beat: float) -> float:
        """
        将谱面中的 Beat (节拍) 转换为实际的时间 (毫秒)。
        需要考虑谱面中可能存在的 BPM 变化。
        """
        if not self._bpms:
            return 0

        def _get_time_for_section(
            bpm: float, previous_bpm_beat: float, current_bpm_beat: float
        ) -> float:
            return (current_bpm_beat - previous_bpm_beat) * (60.0 / bpm) if bpm else 0

        time_ = 0.0

        previous_bpm_beat = 0.0
        current_bpm = 0.0

        for bpm, bpm_beat in self._bpms:
            if bpm_beat > beat:
                break
            time_ += _get_time_for_section(current_bpm, previous_bpm_beat, bpm_beat)
            previous_bpm_beat = bpm_beat
            current_bpm = bpm

        time_ += _get_time_for_section(current_bpm, previous_bpm_beat, beat)

        return time_ * 1000

    def _process_time_chart(self):
        """
        预处理谱面数据：
        1. 提取 BPM 变化信息。
        2. 将所有 Note 的 Beat 转换为 Time。
        3. 为 Note 分配索引。
        """
        checkpoint_index = -1
        note_index = -1

        def get_checkpoint_index():
            nonlocal checkpoint_index
            checkpoint_index += 1
            return checkpoint_index

        def get_note_index():
            nonlocal note_index
            note_index += 1
            return note_index

        for _, note in enumerate(self._chart_data):
            note_type = note["type"]

            if note_type == "BPM":
                bpm = note["bpm"]
                beat = note["beat"]
                self._bpms.append((bpm, beat))
            elif note_type in ["Single", "Directional"]:
                note["time"] = self._beat_to_time(note["beat"])
                note["checkpoint_index"] = get_checkpoint_index()
                note["index"] = get_note_index()
            elif note_type in ["Slide", "Long"]:
                note["index"] = get_note_index()
                note["connections"] = [c for c in note["connections"] if not c.get("hidden", False)]
                for connection in note["connections"]:
                    connection["time"] = self._beat_to_time(connection["beat"])
                    connection["checkpoint_index"] = get_checkpoint_index()
            else:
                self._logger.warning(
                    f"_chart_to_time_chart: Unknown type: {note_type}, Skipped"
                )
        self._logger.debug(
            f"_chart_to_time_chart: Succeed: {len(self._chart_data)} notes"
        )

    def _process_ournotes_time_chart(self):
        """
        预处理 our notes 谱面数据：
        1. 提取 BPM 变化信息。
        2. 将所有 Note 的 tick 转换为 Time (ms)。
        """
        score = self._chart_data.get("score", {})
        events = score.get("events", {})
        
        # 1. 提取 BPM
        bpm_events = events.get("bpm", [])
        if not bpm_events:
            # 默认 BPM
            self._bpms = [(0, 190)]
        else:
            self._bpms = [(b.get("t", 0), b.get("bpm", 190)) for b in bpm_events]
            if self._bpms[0][0] != 0:
                self._bpms.insert(0, (0, self._bpms[0][1]))
                
        # 2. 转换 tick 到 ms 的辅助函数
        def tick_to_ms(t):
            seg = self._bpms
            i = 0
            for k in range(len(seg)):
                if seg[k][0] <= t:
                    i = k
            acc = 0
            for k in range(1, i + 1):
                acc += (seg[k][0] - seg[k-1][0]) * 60000 / (seg[k-1][1] * 480)
            return acc + (t - seg[i][0]) * 60000 / (seg[i][1] * 480)

        # 3. 处理所有 notes
        notes = score.get("notes", [])
        for i, note in enumerate(notes):
            note["index"] = i
            note_type = note.get("type", "tap")
            
            if note_type in ["tap", "flick", "trace"]:
                note["time"] = tick_to_ms(note["t"])
            elif note_type in ["long", "guide"]:
                for node in note.get("node", []):
                    node["time"] = tick_to_ms(node["t"])
                    
        self._logger.debug(f"_process_ournotes_time_chart: Succeed: {len(notes)} notes")

    def notes_to_actions(
        self,
        screen_resolution: tuple[int, int],
        default_move_slice_size,
    ):
        """
        将 Note 转换为具体的手指动作 (down, move, up, wait)。
        """
        if self.game_type == "ournotes":
            self._ournotes_to_actions(screen_resolution, default_move_slice_size)
            return
            
        notes: list[dict] = self._chart_data

        def get_lane_position(lane: int) -> tuple[int, int]:
            """获取指定轨道的屏幕坐标"""
            lane_config = util.get_runtime_info(screen_resolution, self.game_type)["lane"]
            return (
                lane_config["start_x"] + (lane + 0.5) * lane_config["w"],
                lane_config["h"],
            )

        actions = []
        # 维护 5 个可用手指的状态
        available_fingers = [
            {
                "id": i,
                "occupied_time": [],
            }
            for i in range(1, 6)
        ]

        def get_finger(from_time, to_time) -> int:
            """分配一个在指定时间段内空闲的手指"""
            for finger in available_fingers:
                if any(
                    not (to_time <= occupied_from - 10 or from_time >= occupied_to + 10)
                    for occupied_from, occupied_to in finger["occupied_time"]
                ):
                    continue
                else:
                    finger["occupied_time"].append((from_time, to_time))
                    return finger["id"]
            return None

        def add_tap(note_index, from_time, duration, pos):
            """添加一个点击动作 (down -> up)"""
            finger = get_finger(from_time, from_time + duration)
            actions.extend(
                [
                    {
                        "finger": finger,
                        "type": "down",
                        "time": from_time,
                        "pos": pos,
                        "note": note_index,
                    },
                    {
                        "finger": finger,
                        "type": "up",
                        "time": from_time + duration,
                        "note": note_index,
                    },
                ]
            )

        def split_number(num, part_size):
            """将一个数字分割成多个小块，用于平滑移动"""
            result = []
            cur = 0
            while True:
                if num - cur > part_size:
                    result.append((cur, part_size))
                    cur += part_size
                else:
                    result.append((cur, num - cur))
                    break
            return result

        def add_smooth_move(
            note_index,
            finger,
            from_time,
            duration,
            from_,
            to,
            slice_size=default_move_slice_size,
            down=True,
            up=True,
        ):
            """添加一个平滑移动动作 (将长距离移动拆分为多个小 move)"""
            to_time = from_time + duration
            from_x, from_y = from_
            to_x, to_y = to
            slices = split_number(duration, slice_size)
            x_size = (to_x - from_x) / duration
            y_size = (to_y - from_y) / duration

            result = []
            if down:
                result.append(
                    {
                        "finger": finger,
                        "type": "down",
                        "time": from_time,
                        "pos": from_,
                        "note": note_index,
                    }
                )
            
            # 重新设计平滑移动逻辑：基于距离插值，而不是纯基于时间切片
            # 这样可以保证无论持续时间多长，都不会生成过多的 move 指令
            
            # 计算总移动距离
            import math
            total_distance = math.hypot(to_x - from_x, to_y - from_y)
            
            # 设定每次 move 的最小距离阈值 (例如 20 像素)
            min_move_distance = 20.0
            
            if total_distance <= min_move_distance:
                # 如果总距离很短，只需要在结束时发送一个 move
                result.append(
                    {
                        "finger": finger,
                        "type": "move",
                        "time": to_time,
                        "to": (to_x, to_y),
                        "note": note_index,
                    }
                )
            else:
                # 根据距离计算需要多少个切片
                num_slices = int(total_distance / min_move_distance)
                
                # 限制最大切片数量，防止极端情况
                max_slices = int(duration / slice_size)
                if max_slices > 0 and num_slices > max_slices:
                    num_slices = max_slices
                
                if num_slices <= 0:
                    num_slices = 1
                    
                time_step = duration / num_slices
                x_step = (to_x - from_x) / num_slices
                y_step = (to_y - from_y) / num_slices
                
                for i in range(1, num_slices + 1):
                    target_time = from_time + i * time_step
                    target_x = from_x + i * x_step
                    target_y = from_y + i * y_step
                    
                    # 确保最后一个点精确到达目标位置和时间
                    if i == num_slices:
                        target_time = to_time
                        target_x = to_x
                        target_y = to_y
                        
                    result.append(
                        {
                            "finger": finger,
                            "type": "move",
                            "time": target_time,
                            "to": (target_x, target_y),
                            "note": note_index,
                        }
                    )

            if up:
                result.append(
                    {
                        "finger": finger,
                        "type": "up",
                        "time": to_time,
                        "note": note_index,
                    },
                )
            actions.extend(result)

        # 遍历所有 Note，生成对应的动作
        for note in notes:
            note_data = note
            note_type = note_data["type"]
            note_index = note_data.get("index", None)

            if note_type == "Single":
                time_ = note_data["time"]
                from_lane = note_data["lane"]
                pos = get_lane_position(from_lane)

                if note_data.get("flick"):
                    # 粉条 (Flick)：点击并向上滑动
                    finger = get_finger(time_, time_ + 80)
                    add_smooth_move(
                        note_index, finger, time_, 80, pos, (pos[0], pos[1] - 300)
                    )
                else:
                    # 普通蓝键：点击
                    add_tap(note_index, time_, 50, pos)

            elif note_type == "Directional":
                # 方向键 (Directional)：向左或向右滑动
                time_ = note_data["time"]
                fromlane = note_data["lane"]
                width = note_data["width"]
                direction = note_data["direction"]
                if direction == "Right":
                    tolane = fromlane + width
                else:
                    tolane = fromlane - width
                finger = get_finger(time_, time_ + 80)

                add_smooth_move(
                    note_index,
                    finger,
                    time_,
                    80,
                    get_lane_position(fromlane),
                    get_lane_position(tolane),
                )
            elif note_type in ["Long", "Slide"]:
                # 绿条 (Long/Slide)：按下，可能包含移动，最后抬起或滑动
                from_lane = note_data["connections"][0]["lane"]
                from_pos = get_lane_position(from_lane)
                from_time = note_data["connections"][0]["time"]
                to_time = note_data["connections"][-1]["time"]

                end_flick = note_data["connections"][-1].get("flick")
                if end_flick:
                    finger_end_time = to_time + 80
                else:
                    finger_end_time = to_time
                finger = get_finger(
                    from_time,
                    finger_end_time,
                )
                actions.append(
                    {
                        "finger": finger,
                        "type": "down",
                        "time": from_time,
                        "pos": from_pos,
                        "note": note_index,
                    }
                )

                end_pos = None

                for i, connection in enumerate(note_data["connections"]):
                    if i != len(note_data["connections"]) - 1:
                        next_connection = note_data["connections"][i + 1]
                        if connection["lane"] != next_connection["lane"]:
                            # 节点之间有横向移动
                            add_smooth_move(
                                note_index,
                                finger,
                                connection["time"],
                                next_connection["time"] - connection["time"],
                                get_lane_position(connection["lane"]),
                                get_lane_position(next_connection["lane"]),
                                down=False,
                                up=False,
                            )
                    else:
                        end_pos = get_lane_position(connection["lane"])

                if end_flick:
                    # 绿条尾部带粉条
                    add_smooth_move(
                        note_index,
                        finger,
                        to_time,
                        80,
                        end_pos,
                        (end_pos[0], end_pos[1] - 300),
                        down=False,
                        up=False,
                    )
                actions.append(
                    {
                        "finger": finger,
                        "type": "up",
                        "time": finger_end_time,
                        "note": note_index,
                    }
                )
            else:
                logging.warning(f"notes_to_actions: Unknown type: {note_type}")

        # 按时间排序所有动作
        actions.sort(key=lambda x: x["time"])
        actions: list[dict]

        [
            action.setdefault("index", index)
            for index, action in enumerate(actions)
        ]
        self.actions = actions

    def _ournotes_to_actions(self, screen_resolution: tuple[int, int], default_move_slice_size):
        """
        将 our notes 的 Note 转换为具体的手指动作。
        """
        notes: list[dict] = self._chart_data.get("score", {}).get("notes", [])
        
        def get_lane_position(pos, size=6.0) -> tuple[int, int]:
            """
            获取 our notes 轨道的屏幕坐标。
            our notes 的 pos 范围是 0-24。
            """
            # 强制转换为 float，防止 JSON 中的值为字符串
            pos = float(pos)
            size = float(size)
            
            lane_config = util.get_runtime_info(screen_resolution, self.game_type)["lane"]
            
            # 计算音符中心点的 X 坐标
            center_pos = pos + size / 2.0
            x = lane_config["start_x"] + center_pos * lane_config["w"]
            
            y = lane_config["h"]
            return (int(x), int(y))

        actions = []
        # 维护 5 个可用手指的状态
        available_fingers = [
            {
                "id": i,
                "occupied_time": [],
            }
            for i in range(1, 6)
        ]

        def get_finger(from_time, to_time) -> int:
            """分配一个在指定时间段内空闲的手指"""
            for finger in available_fingers:
                if any(
                    not (to_time <= occupied_from - 10 or from_time >= occupied_to + 10)
                    for occupied_from, occupied_to in finger["occupied_time"]
                ):
                    continue
                else:
                    finger["occupied_time"].append((from_time, to_time))
                    return finger["id"]
            return None

        def add_tap(note_index, from_time, duration, pos):
            finger = get_finger(from_time, from_time + duration)
            if finger is None:
                return # 忽略无法分配手指的音符
            actions.extend([
                {"finger": finger, "type": "down", "time": from_time, "pos": pos, "note": note_index},
                {"finger": finger, "type": "up", "time": from_time + duration, "note": note_index},
            ])

        def add_smooth_move(note_index, finger, from_time, duration, from_, to, slice_size=default_move_slice_size, down=True, up=True):
            to_time = from_time + duration
            from_x, from_y = from_
            to_x, to_y = to
            
            result = []
            if down:
                result.append({"finger": finger, "type": "down", "time": from_time, "pos": from_, "note": note_index})
            
            import math
            total_distance = math.hypot(to_x - from_x, to_y - from_y)
            min_move_distance = 20.0
            
            if total_distance <= min_move_distance:
                result.append({"finger": finger, "type": "move", "time": to_time, "to": (to_x, to_y), "note": note_index})
            else:
                num_slices = int(total_distance / min_move_distance)
                max_slices = int(duration / slice_size)
                if max_slices > 0 and num_slices > max_slices:
                    num_slices = max_slices
                if num_slices <= 0:
                    num_slices = 1
                    
                time_step = duration / num_slices
                x_step = (to_x - from_x) / num_slices
                y_step = (to_y - from_y) / num_slices
                
                for i in range(1, num_slices + 1):
                    target_time = from_time + i * time_step
                    target_x = from_x + i * x_step
                    target_y = from_y + i * y_step
                    if i == num_slices:
                        target_time = to_time
                        target_x = to_x
                        target_y = to_y
                    result.append({"finger": finger, "type": "move", "time": target_time, "to": (target_x, target_y), "note": note_index})

            if up:
                result.append({"finger": finger, "type": "up", "time": to_time, "note": note_index})
            actions.extend(result)

        # 预处理 long note 的接力关系 (seamNext/seamPrev)
        # 参考 moenote/index.html 中的逻辑
        heads = {}
        for note in notes:
            if note.get("type") == "long":
                t = note["node"][0]["t"]
                if t not in heads:
                    heads[t] = []
                heads[t].append(note)
                
        for note in notes:
            if note.get("type") == "long":
                t_end = note["node"][-1]["t"]
                candidates = heads.get(t_end, [])
                for c in candidates:
                    if c != note and c["node"][-1]["t"] > t_end:
                        note["seamNext"] = c
                        c["seamPrev"] = note
                        break

        # 遍历生成动作
        for note in notes:
            note_type = note.get("type", "tap")
            note_index = note["index"]
            
            if note_type == "guide":
                continue # 忽略 guide 线
                
            if note_type in ["tap", "trace"]:
                time_ = note["time"]
                pos = get_lane_position(note["pos"], note.get("size", 6.0))
                add_tap(note_index, time_, 50, pos)
                
            elif note_type == "flick":
                time_ = note["time"]
                pos = get_lane_position(note["pos"], note.get("size", 6.0))
                finger = get_finger(time_, time_ + 80)
                if finger:
                    # 默认向上滑动
                    to_pos = (pos[0], pos[1] - 300)
                    dir_ = note.get("dir")
                    if dir_ == "left":
                        to_pos = (pos[0] - 200, pos[1] - 200)
                    elif dir_ == "right":
                        to_pos = (pos[0] + 200, pos[1] - 200)
                    add_smooth_move(note_index, finger, time_, 80, pos, to_pos)
                    
            elif note_type == "long":
                # 如果是接力的后续段，跳过，由头部统一处理
                if note.get("seamPrev"):
                    continue
                    
                # 收集整条接力长条的所有节点
                chain_nodes = []
                curr = note
                while curr:
                    chain_nodes.extend(curr["node"])
                    curr = curr.get("seamNext")
                    
                if not chain_nodes:
                    continue
                    
                from_time = chain_nodes[0]["time"]
                to_time = chain_nodes[-1]["time"]
                
                end_flick = chain_nodes[-1].get("type") == "flick"
                finger_end_time = to_time + 80 if end_flick else to_time
                
                finger = get_finger(from_time, finger_end_time)
                if not finger:
                    continue
                    
                from_pos = get_lane_position(chain_nodes[0]["pos"], chain_nodes[0].get("size", 6.0))
                actions.append({"finger": finger, "type": "down", "time": from_time, "pos": from_pos, "note": note_index})
                
                end_pos = None
                last_valid_pos = chain_nodes[0]["pos"]
                last_valid_size = chain_nodes[0].get("size", 6.0)
                
                for i in range(len(chain_nodes) - 1):
                    curr_node = chain_nodes[i]
                    next_node = chain_nodes[i+1]
                    
                    # 处理 pos 为 'auto' 的情况，继承上一个有效节点的位置
                    curr_node_pos = curr_node["pos"] if curr_node["pos"] != "auto" else last_valid_pos
                    curr_node_size = curr_node.get("size", 6.0) if curr_node.get("size", 6.0) != "auto" else last_valid_size
                    
                    next_node_pos = next_node["pos"] if next_node["pos"] != "auto" else curr_node_pos
                    next_node_size = next_node.get("size", 6.0) if next_node.get("size", 6.0) != "auto" else curr_node_size
                    
                    # 更新 last_valid
                    last_valid_pos = next_node_pos
                    last_valid_size = next_node_size
                    
                    curr_pos = get_lane_position(curr_node_pos, curr_node_size)
                    next_pos = get_lane_position(next_node_pos, next_node_size)
                    
                    if curr_pos[0] != next_pos[0]:
                        add_smooth_move(
                            note_index, finger, curr_node["time"], next_node["time"] - curr_node["time"],
                            curr_pos, next_pos, down=False, up=False
                        )
                    end_pos = next_pos
                    
                if end_pos is None:
                    end_pos = from_pos
                    
                if end_flick:
                    dir_ = chain_nodes[-1].get("dir")
                    to_pos = (end_pos[0], end_pos[1] - 300)
                    if dir_ == "left":
                        to_pos = (end_pos[0] - 200, end_pos[1] - 200)
                    elif dir_ == "right":
                        to_pos = (end_pos[0] + 200, end_pos[1] - 200)
                    add_smooth_move(note_index, finger, to_time, 80, end_pos, to_pos, down=False, up=False)
                    
                actions.append({"finger": finger, "type": "up", "time": finger_end_time, "note": note_index})

        actions.sort(key=lambda x: x["time"])
        for index, action in enumerate(actions):
            action.setdefault("index", index)
        self.actions = actions

    def actions_to_MNTcmd(self, resolution, orientation, offset_info):
        """
        将动作列表转换为 minitouch 命令队列。
        每个元素为 (absolute_time_ms, command_string)
        """
        self.command_queue = []
        
        up_offset = offset_info.get("up", 0)
        down_offset = offset_info.get("down", 0)
        move_offset = offset_info.get("move", 0)

        def round_tuple(target):
            return tuple(round(x) for x in target)

        # 按时间分组动作
        grouped_actions = {}
        for action in self.actions:
            action_type = action["type"]
            target_time = action["time"]
            
            if action_type == "down":
                target_time += down_offset
            elif action_type == "move":
                target_time += move_offset
            elif action_type == "up":
                target_time += up_offset
                
            # 精度控制在 5ms 内的合并，减少 commit 频率，防止 Minitouch 缓冲区阻塞
            time_key = round(target_time / 5) * 5
            if time_key not in grouped_actions:
                grouped_actions[time_key] = []
            grouped_actions[time_key].append(action)
            
        for time_key in sorted(grouped_actions.keys()):
            actions_in_group = grouped_actions[time_key]
            
            # 指令微调 (Micro-delay)：
            # 如果同一时间窗内有多个动作，为了防止 Android 底层 input 系统并发阻塞（导致偶发几百毫秒的尖峰延迟），
            # 我们将它们拆分，并在时间轴上人为错开 2 毫秒。
            # 优先处理 move，然后是 down，最后是 up，以符合物理逻辑。
            
            # 按类型分类
            moves = [a for a in actions_in_group if a["type"] == "move"]
            downs = [a for a in actions_in_group if a["type"] == "down"]
            ups = [a for a in actions_in_group if a["type"] == "up"]
            
            # 重新排序：down -> move -> up
            sorted_actions = downs + moves + ups
            
            # 将同一时间窗内的动作合并为一个指令块，统一 commit
            # 这对于保持多点触控（如长条）的状态连续性至关重要
            cmd_str_parts = []
            for action in sorted_actions:
                action_type = action["type"]
                finger = action["finger"]
                
                if action_type == "down":
                    x, y = util.androidxy_to_MNTxy(
                        round_tuple(action["pos"]), resolution, orientation
                    )
                    cmd_str_parts.append(f"d {finger} {x} {y} 1\n")
                elif action_type == "move":
                    x, y = util.androidxy_to_MNTxy(
                        round_tuple(action["to"]), resolution, orientation
                    )
                    cmd_str_parts.append(f"m {finger} {x} {y} 1\n")
                elif action_type == "up":
                    cmd_str_parts.append(f"u {finger}\n")
            
            if cmd_str_parts:
                cmd_str_parts.append("c\n")
                final_cmd_str = "".join(cmd_str_parts)
                self.command_queue.append((time_key, final_cmd_str))

    def dump_debug_config(self):
        """导出调试配置，包含谱面数据、动作列表和生成的命令"""
        dump_path = Path("debug/dump")
        dump_path.mkdir(parents=True, exist_ok=True)
        (
            dump_path / f"{self._song_name}-{self._difficulty}-{time.time()}.yml"
        ).write_text(
            yaml.safe_dump(
                {
                    "song_name": self._song_name,
                    "song_id": self._id_,
                    "chart": self._chart_data,
                    "actions": self.actions,
                    "commands": [],
                },
                sort_keys=False,
                allow_unicode=True,
                indent=2,
            ),
            "utf-8",
        )
