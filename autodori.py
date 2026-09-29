import argparse
import datetime
import json
import logging
import random
import re
import string
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Union

import requests

# 初始化目录结构
data_path = Path("data")
data_path.mkdir(exist_ok=True)
cache_path = Path("cache")
cache_path.mkdir(exist_ok=True)
config_path = Path("data/config.yml")
Path("debug").mkdir(exist_ok=True)
if not config_path.exists():
    config_path.touch()
    config_path.write_text("{}", encoding="utf-8")


import numpy as np
from fuzzywuzzy import process as fzwzprocess
from maa.context import Context
from maa.controller import AdbController
from maa.custom_action import CustomAction, CustomRecognitionResult
from maa.custom_recognition import CustomRecognition
from maa.define import RectType
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import AdbDevice, Toolkit
from minitouchpy import (
    MNT,
    MNTEvATive7LogEventData,
    MNTEvent,
    MNTEventData,
    MNTServerCommunicateType,
    CommandBuilder,
)

import player
from api import BestdoriAPI
from chart import Chart, PlayRecord
from util import *

# 全局配置与状态变量
MIN_LIVEBOOST = 1
LIVEMODE = "freelive"
DIFFICULTY = "hard"
GAME_TYPE = "bangdream"
# 延迟补偿数据，用于动态调整触控指令的时间
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
PHOTOGATE_LATENCY = 30 # 光电门延迟（等待第一个Note落下的额外延迟）
DEFAULT_MOVE_SLICE_SIZE = 25 # 滑动操作的切片大小，增大以降低 Socket 通信压力
MAX_FAILED_TIMES = 10 # 最大连续失败次数
CMD_SLICE_SIZE = 100 # 每次发送给 minitouch 的命令批次大小

config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
maaresource = Resource()
maatasker = Tasker()
maacontroller: AdbController = None
device: AdbDevice = None
current_player: player.Player = None
current_orientation: int = 0
mnt: MNT = None
all_songs: dict = BestdoriAPI.get_song_list()
# 构建歌曲名到歌曲ID的映射字典
all_song_name_indexes: dict[str, str] = {
    list(filter(lambda title: title is not None, sinfo["musicTitle"]))[0]: sid
    for sid, sinfo in all_songs.items()
}
current_song_name: str = None
current_song_id: str = None
current_chart: Chart = None
play_failed_times: int = 0
stop_playing_flag: bool = False
callback_data: dict = {}
callback_data_lock = threading.Lock()
cmd_log_list: list[MNTEvATive7LogEventData] = []
cmd_log_list_lock = threading.Lock()
current_version = None


def reset_callback_data():
    """重置 minitouch 回调统计数据，用于计算延迟补偿"""
    global callback_data
    callback_data = {
        "wait": {"total": 0, "total_offset": 0.0},
        "move": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "up": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "down": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "interval": {"total": 0, "total_offset": 0.0},
        "last_cmd_endtime": -1,
    }


reset_callback_data()


def check_song_available(name, id_, difficulty):
    """检查歌曲是否可用（例如之前失败过的歌曲）"""
    lastmatched = PlayRecord.get_or_none(chart_id=id_, difficulty=difficulty)
    if lastmatched:
        if not lastmatched.succeed:
            return True

    return True


@maaresource.custom_recognition("SongRecognition")
class SongRecognition(CustomRecognition):
    """
    MAA 自定义识别：识别当前选中的歌曲名称。
    使用 OCR 识别屏幕上的歌曲名，并使用 fuzzywuzzy 进行模糊匹配。
    """
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        if GAME_TYPE == "ournotes":
            roi = [781, 428, 494, 35] # our notes 选歌界面歌曲名 ROI 占位符
        else:
            if LIVEMODE == "multilive":
                roi = [111, 543, 453, 34] # 占位符：协力模式下歌曲名所在的屏幕区域
            else:
                roi = [200, 332, 368, 29] # 单人模式歌曲名所在的屏幕区域

        def match(model=None):
            pplname = "_ocrsong_" + "".join(random.choices(string.ascii_lowercase, k=7))
            pipeline = {
                pplname: {
                    "recognition": "OCR",
                    "only_rec": True,
                    "roi": roi,
                },
            }
            if model != None:
                pipeline[pplname]["model"] = model
            try:
                song_fuzzyname = context.run_recognition(
                    pplname,
                    argv.image,
                    pipeline,
                ).best_result.text
            except:
                song_fuzzyname = ""
            return fuzzy_match_song(song_fuzzyname)

        # 使用默认模型进行识别（ppocr_v6/medium 已原生支持多语言）
        commonmatch = match()
        logging.debug(
            "Match result with default model: {}".format(
                commonmatch
            )
        )
        
        if commonmatch[1] < 50:
            return CustomRecognition.AnalyzeResult(None, "")
        result_music_name = commonmatch[0]

        if GAME_TYPE != "ournotes":
            if not check_song_available(
                result_music_name, all_song_name_indexes[result_music_name], DIFFICULTY
            ):
                return CustomRecognition.AnalyzeResult(None, "")

        return CustomRecognition.AnalyzeResult(roi, result_music_name)


@maaresource.custom_recognition("LiveBoostEnoughRecognition")
class LiveBoostEnoughRecognition(CustomRecognition):
    """
    MAA 自定义识别：识别当前体力 (Live Boost) 是否充足。
    """
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        if GAME_TYPE == "ournotes":
            roi = [1044, 24, 69, 26] # our notes 体力显示 ROI 占位符
        else:
            # roi = [970, 29, 39, 21]
            roi = [979, 30, 61, 20]

        pipeline = {
            "live_boost_enough_ocr": {
                "recognition": "OCR",
                "only_rec": True,
                "roi": roi,
            },
        }
        live_boost = context.run_recognition(
            "live_boost_enough_ocr",
            argv.image,
            pipeline,
        ).best_result.text

        logging.debug("Live boost rec result: {}".format(live_boost))
        pattern = r"^\s*(\d+)\s*/"
        match = re.match(pattern, live_boost.replace(" ", ""))

        if match:
            try:
                live_boost = int(match.group(1))
            except:
                live_boost = -1
        else:
            live_boost = -1

        logging.debug("Live boost: {}".format(live_boost))
        return CustomRecognition.AnalyzeResult(roi, str(live_boost))


@maaresource.custom_action("HandleLiveBoost")
class HandleLiveBoost(CustomAction):
    """
    MAA 自定义动作：处理体力不足的情况。
    如果体力低于设定值，则关闭应用并停止脚本。
    """
    def run(self, context: Context, argv: CustomAction.RunArg):
        liveboost = int(argv.reco_detail.best_result.detail)
        if liveboost < MIN_LIVEBOOST:
            logging.debug("Live boost not enough, ready to exit")
            context.run_action("close_app")
            context.run_action("stop")
        return CustomAction.RunResult(True)


@maaresource.custom_recognition("PlayResultRecognition")
class PlayResultRecognition(CustomRecognition):
    """
    MAA 自定义识别：识别打歌结算界面的成绩（Perfect, Great, Miss 等）。
    """
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        if GAME_TYPE == "ournotes":
            types = {
                "score": {"roi": [100, 100, 50, 20]}, # 占位符
                "maxcombo": {"roi": [100, 130, 50, 20]}, # 占位符
                "perfect": {"roi": [100, 160, 50, 20]}, # 占位符
                "great": {"roi": [100, 190, 50, 20]}, # 占位符
                "good": {"roi": [100, 220, 50, 20]}, # 占位符
                "bad": {"roi": [100, 250, 50, 20]}, # 占位符
                "miss": {"roi": [100, 280, 50, 20]}, # 占位符
                "fast": {"roi": [100, 310, 50, 20]}, # 占位符
                "slow": {"roi": [100, 340, 50, 20]}, # 占位符
            }
        else:
            types = {
                "score": {
                    "roi": [1028, 192, 144, 35],
                },
                "maxcombo": {
                    "roi": [1009, 391, 91, 28],
                },
                "perfect": {
                    "roi": [829, 282, 90, 28],
                },
                "great": {
                    "roi": [828, 322, 91, 27],
                },
                "good": {
                    "roi": [829, 363, 91, 27],
                },
                "bad": {
                    "roi": [829, 401, 90, 27],
                },
                "miss": {
                    "roi": [830, 438, 91, 28],
                },
                "fast": {
                    "roi": [1088, 283, 90, 27],
                },
                "slow": {
                    "roi": [1088, 323, 91, 28],
                },
            }
        result = {type_: {} for type_ in types.keys()}
        pipeline = {
            f"_PlayResultRecognition_ocr_{type_}": {
                "recognition": "OCR",
                "only_rec": True,
                "roi": type_value["roi"],
            }
            for type_, type_value in types.items()
        }
        for type_, _ in types.items():
            try:
                ocrtext = context.run_recognition(
                    f"_PlayResultRecognition_ocr_{type_}",
                    argv.image,
                    pipeline,
                ).best_result.text
                type_result = int(ocrtext)
            except:
                type_result = -1
            result[type_] = type_result

        logging.debug("Play result: {}".format(result))
        return CustomRecognition.AnalyzeResult([0, 0, 0, 0], json.dumps(result))


@maaresource.custom_action("SavePlayResult")
class SavePlayResult(CustomAction):
    """
    MAA 自定义动作：保存打歌结果到数据库。
    """
    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            global current_song_id, play_failed_times
            succeed: bool = json.loads(argv.custom_action_param).get("succeed")
            if succeed:
                play_failed_times = 0
                playresult = argv.reco_detail.best_result.detail
                if isinstance(playresult, str):
                    playresult = json.loads(argv.reco_detail.best_result.detail)
            else:
                play_failed_times += 1
                playresult = {}
            PlayRecord.create(
                play_time=int(time.time()),
                play_offset=OFFSET,
                result=playresult,
                succeed=succeed,
                chart_id=current_song_id,
                difficulty=DIFFICULTY,
            )
            if play_failed_times >= MAX_FAILED_TIMES:
                logging.error("Failed attempts exceed max failed times")
                context.run_action("close_app")
                context.run_action("stop")
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"Failed to save play result: {e}")
            return CustomAction.RunResult(False)


@maaresource.custom_action("Play")
class Play(CustomAction):
    """
    MAA 自定义动作：执行打歌逻辑。
    """
    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            # 将 play_song 放入后台线程执行，避免阻塞 MAA 流水线
            # 这样流水线才能继续截图并识别 "演出失败" 或 "结算画面"
            play_thread = threading.Thread(target=play_song, daemon=True)
            play_thread.start()
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"Failed when start play song thread: {e}", stack_info=True)
            return CustomAction.RunResult(False)


@maaresource.custom_action("StopPlay")
class StopPlay(CustomAction):
    """
    MAA 自定义动作：停止当前打歌。
    """
    def run(self, context: Context, argv: CustomAction.RunArg):
        global stop_playing_flag
        stop_playing_flag = True
        logging.info("StopPlay action triggered by pipeline.")
        return CustomAction.RunResult(True)


@maaresource.custom_action("SaveSong")
class SaveSong(CustomAction):
    """
    MAA 自定义动作：保存当前识别到的歌曲信息，并准备谱面数据。
    """
    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            name: CustomRecognitionResult = argv.reco_detail.best_result.detail
            save_song(name)
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"Failed to save song: {e}", exc_info=True)
            return CustomAction.RunResult(False)


def fuzzy_match_song(name):
    """使用 fuzzywuzzy 模糊匹配歌曲名"""
    if GAME_TYPE == "ournotes":
        try:
            with open("moenote/index/songs.json", "r", encoding="utf-8") as f:
                songs_data = json.load(f)
            song_names = [info.get("title") for info in songs_data.values() if info.get("title")]
            return fzwzprocess.extractOne(name, song_names)
        except Exception as e:
            logging.error(f"Failed to load ournotes songs.json for fuzzy match: {e}")
            return None, 0
    else:
        return fzwzprocess.extractOne(name, list(all_song_name_indexes.keys()))


def _get_orientation():
    """
    获取设备当前的屏幕方向。
    0: 0°, 1: 90°, 2: 180°, 3: 270°
    """
    try:
        command_list = [
            str(device.adb_path.absolute()),
            "-s",
            device.address,
            "shell",
            "dumpsys input|grep SurfaceOrientation",
        ]

        logging.debug(
            "get SurfaceOrientation command: {}".format(" ".join(command_list))
        )
        output = subprocess.check_output(command_list, text=True)
        match = re.search(r"SurfaceOrientation:\s*(\d+)", output)
        orientation = int(match.group(1))
        logging.debug("SurfaceOrientation: {}".format(orientation))
        return orientation
    except Exception as e:
        logging.error(f"Failed to get SurfaceOrientation: {e}")
        return 0


def save_song(name):
    """
    保存当前歌曲信息，初始化 Chart 对象，并将谱面转换为 minitouch 命令。
    """
    global current_song_name, current_song_id, current_chart, current_orientation
    # 移除 OCR 结果中可能包含的首尾双引号或单引号，但保留歌曲名本身包含的引号
    if (name.startswith('"') and name.endswith('"')) or (name.startswith("'") and name.endswith("'")):
        current_song_name = name[1:-1]
    else:
        current_song_name = name
        
    if GAME_TYPE == "ournotes":
        try:
            with open("moenote/index/songs.json", "r", encoding="utf-8") as f:
                songs_data = json.load(f)
                
            # name 已经是 fuzzy_match_song 匹配好的结果，直接反查 ID
            for sid, info in songs_data.items():
                if info.get("title") == current_song_name:
                    current_song_id = sid
                    break
            
            if not current_song_id:
                logging.error(f"Could not find song ID for: {current_song_name}")
                return
                
        except Exception as e:
            logging.error(f"Failed to load ournotes songs.json: {e}")
            return
            
        current_chart = Chart((current_song_id, DIFFICULTY), current_song_name, game_type="ournotes")
    else:
        current_song_id = all_song_name_indexes[current_song_name]
        current_chart = Chart((current_song_id, DIFFICULTY), current_song_name)
        
    current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE)
    current_orientation = _get_orientation()
    current_chart.actions_to_MNTcmd(
        (mnt.max_x, mnt.max_y), current_orientation, OFFSET
    )
    logging.debug("Save song: {}".format(name))


def play_song():
    """
    核心打歌循环。
    等待第一个 Note 落下后，开始基于绝对时间的高精度轮询发送 minitouch 命令。
    """
    global stop_playing_flag
    stop_playing_flag = False
    logging.info("Start play")
    
    if current_chart is None:
        logging.error("current_chart is None! save_song() might not have been called.")
        raise ValueError("current_chart is not initialized.")
        
    if not hasattr(current_chart, 'command_queue') or current_chart.command_queue is None:
        logging.error("current_chart.command_queue is None! actions_to_MNTcmd() might have failed.")
        raise ValueError("command_queue is not initialized.")

    cmd_log_list.clear()
    reset_callback_data()

    # 阻塞等待第一个 Note 落下
    wait_first_note()

    import gc
    gc.disable() # 禁用垃圾回收，防止打歌过程中出现卡顿

    # 获取队列中第一个动作的理论时间 (毫秒)
    first_note_time_ms = current_chart.command_queue[0][0]
    
    # 反推音乐真正的开始时间，并加上光电门固有的物理延迟补偿
    start_time = time.perf_counter() - (first_note_time_ms / 1000.0) + (PHOTOGATE_LATENCY / 1000.0)
    
    queue_index = 0
    queue_length = len(current_chart.command_queue)

    # 高精度轮询发送命令
    while queue_index < queue_length:
        if stop_playing_flag:
            logging.info("Play stopped by StopPlay action.")
            # 释放所有手指
            mnt.send("u 1\nu 2\nu 3\nu 4\nu 5\nc\n")
            break

        target_time_ms, cmd_str = current_chart.command_queue[queue_index]
        target_time_s = target_time_ms / 1000.0
        
        while True:
            current_time_s = time.perf_counter() - start_time
            delta = target_time_s - current_time_s
            
            if delta > 0.002:
                time.sleep(delta - 0.002)
            elif delta <= 0:
                mnt.send(cmd_str)
                queue_index += 1
                break

    gc.enable()
    gc.collect()
    time.sleep(2)


def wait_first_note():
    """
    通过不断截图并比较指定区域的颜色变化，来判断第一个 Note 是否落下。
    这是同步谱面时间和游戏画面的关键步骤。
    """
    last_color = None
    waited_frames = 0
    info = get_runtime_info(current_player.resolution, GAME_TYPE)["wait_first"]
    from_row, to_row = info["from"], info["to"]
    freezed = False
    consecutive_failures = 0 # 记录连续截图失败的次数

    while True:
        try:
            screen = current_player.ipc_capture_display()
            if screen is None:
                consecutive_failures += 1
                if consecutive_failures > 10: # 连续失败 10 次才认为彻底断开
                    logging.error("Failed to get screen consecutively, aborting wait_first_note.")
                    break
                time.sleep(0.1) # 稍微等待一下再试
                continue
                
            consecutive_failures = 0 # 截图成功，重置失败计数

            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)

            if last_color is not None:
                change_score = np.sum(cur_color[0:3] - last_color[0:3])
                logging.debug(f"Picture changed: {change_score}")
                if change_score > 3:
                    if freezed:
                        logging.debug(
                            f"The first note falls between {from_row}-{to_row}"
                        )
                        # 绝对时间方案中，延迟补偿在 start_time 计算时处理，这里不再 sleep
                        break
                else:
                    if not freezed:
                        waited_frames += 1

                # 如果画面长时间未变化，认为游戏已加载完毕，等待 Note 落下
                if not freezed and waited_frames >= 200:
                    freezed = True
                    logging.debug("Picture freezed, waiting for the first note...")

            last_color = cur_color
        except Exception as e:
            logging.error(f"Exception in wait_first_note: {e}")
            # 发生其他异常时，也应该容错，而不是立刻退出
            time.sleep(0.1)


def init_maa():
    """初始化 MAA 框架，连接 ADB 设备"""
    user_path = "./"
    resource_path = "assets/resource"

    res_job = maaresource.post_bundle(resource_path)
    res_job.wait()
    Toolkit.init_option(user_path)
    for i in range(3):
        adb_devices = Toolkit.find_adb_devices()
        if adb_devices:
            break
    if not adb_devices:
        logging.fatal("No ADB device found.")
        sys.exit(1)

    global device, maacontroller
    _device: list[AdbDevice] = []
    for device in adb_devices:
        extra_names = device.config.get("extras", {}).keys()
        if "mumu" in extra_names or "ld" in extra_names:
            if (device.name, device.address) not in [
                (d.name, d.address) for d in _device
            ]:
                _device.append(device)
    filter_str = config.get("device", {}).get("filter", "devices")
    _device = eval(filter_str, {}, {"devices": _device})

    if not _device:
        logging.fatal("No supported devices were found.")
        sys.exit(1)
    elif len(_device) == 1:
        device = _device[0]
    elif len(_device) > 1:
        print("Multiple devices were found:")
        for i, device in enumerate(_device):
            print(f"{i}: {device.name}({device.address})")
        selected = input("Select a device: ")
        device = _device[int(selected)]
    maacontroller = AdbController(
        adb_path=device.adb_path,
        address=device.address,
        screencap_methods=device.screencap_methods,
        input_methods=device.input_methods,
        config=device.config,
    )

    for i in range(3):
        if maacontroller.post_connection().wait().succeeded:
            break

    # tasker = Tasker(notification_handler=MyNotificationHandler())
    maatasker.bind(maaresource, maacontroller)

    if not maatasker.inited:
        logging.fatal("Failed to init MAA.")
        sys.exit(1)

    logging.info("MAA inited.")


def mnt_callback(event: MNTEvent, data: MNTEventData):
    """
    minitouch 回调函数。
    收集命令执行的耗时数据，用于动态调整延迟补偿。
    """
    global callback_data
    if event == MNTEvent.EVATIVE7_LOG:
        data: MNTEvATive7LogEventData = data

        cmd = data.cmd
        cost = data.cost

        with cmd_log_list_lock:
            cmd_log_list.append(data)
        cmd_type = cmd.split(" ")[0]

        callback_data_lock.acquire()

        if (last_cmd_endtime := callback_data.get("last_cmd_endtime")) != -1:
            callback_data["interval"]["total"] += 1
            callback_data["interval"]["total_offset"] += (
                data.start_time - last_cmd_endtime
            )
        callback_data["last_cmd_endtime"] = data.end_time
        if cmd_type in ["w"]:
            callback_data["wait"]["total"] += 1
            callback_data["wait"]["total_offset"] += cost - int(cmd.split(" ")[-1])
        elif cmd_type in ["u", "d", "m"]:
            type_ = {
                "u": "up",
                "d": "down",
                "m": "move",
            }[cmd_type]
            callback_data[type_]["uncommited"] += 1
            callback_data[type_]["total"] += 1
            callback_data[type_]["total_offset"] += cost
        elif cmd_type in ["c"]:
            total_uncommited = 0
            for type_ in ["up", "down", "move"]:
                total_uncommited += callback_data[type_]["uncommited"]

            if total_uncommited != 0:
                for type_ in ["up", "down", "move"]:
                    callback_data[type_]["total_offset"] += cost * (
                        callback_data[type_]["uncommited"] / total_uncommited
                    )
                    callback_data[type_]["uncommited"] = 0
        callback_data_lock.release()


def init_player_and_mnt():
    """初始化 Player (用于截图) 和 minitouch (用于触控)"""
    global current_player, mnt

    extra_config = device.config["extras"]
    if "mumu" in extra_config.keys():
        extra_config = extra_config["mumu"]
        type_ = "mumu"
        logging.debug(f"Device name for mumu: {device.name}")
        if "v5" in device.name:
            type_ += "v5"
        elif "MuMuPlayer12" in device.name:
            type_ += "v4"
        logging.debug(f"Resolved player type_: {type_}")
    elif "ld" in extra_config.keys():
        extra_config = extra_config["ld"]
        type_ = "ld"

    path = extra_config["path"]
    index = extra_config["index"]

    current_player = player.Player(type_, Path(path), index)
    mnt = MNT(
        device.address,
        type_="EvATive7",
        communicate_type=MNTServerCommunicateType.STDIO,
        mnt_asset_path=Path("./assets/minitouch_EvATive7"),
        callback=mnt_callback,
        adb_executor=str(device.adb_path.absolute()),
    )

    logging.info("Mumu and MNT inited.")


def configure_log():
    """配置日志输出格式和文件"""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s[%(levelname)s][%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                "debug/autodori-{}.log".format(
                    datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                ),
                mode="w",
                encoding="utf-8",
            ),
        ],
    )


def _get_override_pipeline():
    """
    动态生成 MAA 的 pipeline 配置覆盖。
    根据命令行参数设置难度和游戏模式。
    """
    all_pipelines = {}
    difficulty: str = DIFFICULTY

    if GAME_TYPE == "ournotes":
        # 为 our notes 定义难度选择的 ROI (占位符，需后续根据实际截图调整)
        ournotes_roi = {
            "easy": [781, 562, 58, 19],
            "normal": [898, 562, 58, 19],
            "hard": [1037, 562, 58, 19],
            "expert": [1160, 562, 58, 19],
        }.get(difficulty, [300, 200, 50, 50])
        
        # 覆盖 OurNotes_SetDifficulty 节点
        all_pipelines["OurNotes_SetDifficulty"] = {
            "action": "Click",
            "recognition": "TemplateMatch",
            "template": [
                # 使用 our notes 专属的难度图标路径
                f"ournotes/difficulty/{difficulty}_active.png",
                f"ournotes/difficulty/{difficulty}_inactive.png",
            ],
            "next": "OurNotes_GetSongName",
            "target": ournotes_roi,
            "timeout": 5000,
        }
    else:
        # set_difficulty
        roi = {
            "easy": [659, 495, 107, 97],
            "normal": [768, 494, 107, 97],
            "hard": [886, 494, 105, 97],
            "expert": [996, 493, 107, 97],
            "special": [1086, 449, 192, 184],
        }[difficulty]
        all_pipelines["set_difficulty"] = {
            "action": "Click",
            "recognition": "TemplateMatch",
            "template": [
                f"live/difficulty/{difficulty}_active.png",
                f"live/difficulty/{difficulty}_inactive.png",
            ],
            "next": "get_song_name",
            "target": roi,
            "timeout": 5000,
            "jump_back": ["random_choice_song"],
        }

        # live mode
        livemode_pipeline = {
            "recognition": "OCR",
            "expected": "",
            "roi": [679, 183, 257, 354],
            "action": "Click",
            "post_delay": 1000,
            "next": ["select_song", "select_live_mode", "live_home_button"],
            "jump_back": ["login_expired", "connect_failed"],
        }
        if LIVEMODE == "freelive":
            livemode_pipeline["expected"] = "自由演出"
        elif LIVEMODE == "challengelive":
            livemode_pipeline["expected"] = "挑战演出"
        elif LIVEMODE == "multilive":
            livemode_pipeline["expected"] = "协力演出"
            livemode_pipeline["next"] = ["select_multi_live", "select_live_mode", "live_home_button"]
            
            # 动态注入协力模式的难度选择节点
            multi_roi = {
                "easy": [566, 541, 74, 74],
                "normal": [651, 541, 74, 74],
                "hard": [741, 541, 74, 74],
                "expert": [823, 541, 74, 74],
                "special": [1086, 541, 74, 74],
            }[difficulty]
            all_pipelines["multi_set_difficulty"] = {
                "action": "Click",
                "recognition": "FeatureMatch",
                "template": [
                    f"live/difficulty/{difficulty}_active.png",
                    f"live/difficulty/{difficulty}_inactive.png",
                ],
                "next": "multi_get_song_name",
                "target": multi_roi,
                "timeout": 5000,
                "jump_back": ["multi_random_song"],
            }
            
            # 动态注入协力模式的歌曲名识别节点
            all_pipelines["multi_get_song_name"] = {
                "action": "Custom",
                "recognition": "Custom",
                "custom_recognition": "SongRecognition",
                "custom_action": "SaveSong",
                "next": [
                    "multi_ready"
                ],
                # 注意：这里需要修改 SongRecognition 类以支持传入自定义 ROI，
                # 或者在 SongRecognition 内部根据 LIVEMODE 判断使用哪个 ROI。
                # 目前先在清单中记录需要一个新的 ROI。
            }
            
        all_pipelines["select_live_mode"] = livemode_pipeline

    return all_pipelines


def get_current_version():
    """从 metadata 文件获取当前版本号"""
    global current_version
    try:
        metadata_text = Path("assets/build_metadata.json").read_text(encoding="utf-8")
        metadata = json.loads(metadata_text)
        current_version = metadata["version"]
    except Exception:
        logging.debug("Failed to get current version")


def check_update():
    """检查 GitHub Release 是否有新版本"""
    logging.debug("Checking for updates...")
    try:
        version = requests.get(
            "https://api.github.com/repos/EvATive7/autodori/releases/latest"
        ).json()["tag_name"]
        logging.debug(f"Current version: {current_version}")
        logging.debug(f"Newest version: {version}")
        if compare_semver(version, current_version) == 1:
            ORANGE = "\033[38;5;208m"
            BOLD = "\033[1m"
            RESET = "\033[0m"

            print(
                f"{ORANGE}{BOLD}有更新可用：{version}，在 https://github.com/EvATive7/autodori/releases 下载最新版本{RESET}"
            )
            print(
                f"{ORANGE}{BOLD}An update is available: {version}, download the latest version at https://github.com/EvAtive7/autodori/releases{RESET}"
            )
            time.sleep(5)

    except Exception as e:
        logging.error("failed to check for updates: {}".format(e))


def main():
    """主函数：解析参数，初始化环境，启动 MAA 任务"""
    configure_log()

    parser = argparse.ArgumentParser(
        description="AutoDori script with different modes."
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["main"],
        help="Specify the mode to run",
        default="main",
    )
    parser.add_argument(
        "--game",
        type=str,
        choices=["bangdream", "ournotes"],
        help="Specify the game to run (overrides config.yml)",
        default=None,
    )
    parser.add_argument(
        "--difficulty",
        type=str,
        choices=["easy", "normal", "hard", "expert", "special"],
        help="Specify the difficulty for main mode",
        default="hard",
    )
    parser.add_argument(
        "--livemode",
        type=str,
        choices=["freelive", "challengelive", "multilive"],
        help="Specify the live mode to run",
        default="freelive",
    )
    parser.add_argument(
        "--liveboost",
        type=int,
        default=1,
        help="Specify the min liveboost for main mode. If current liveboost is lower than this value, the script will exit.",
    )
    parser.add_argument(
        "--skip-version-check",
        action="store_true",
        help="Specify if skip version check",
    )
    args = parser.parse_args()

    # 命令行参数优先级高于配置文件
    global GAME_TYPE
    GAME_TYPE = args.game if args.game else config.get("game", "bangdream")
    
    if args.mode == "main":
        if GAME_TYPE == "ournotes":
            entry = "OurNotes_Main"
        else:
            entry = "main"
    else:
        sys.exit(1)

    if not args.skip_version_check:
        get_current_version()
        if current_version != None:
            check_update()

    global DIFFICULTY, MIN_LIVEBOOST, LIVEMODE
    DIFFICULTY = args.difficulty
    LIVEMODE = args.livemode
    MIN_LIVEBOOST = args.liveboost
    init_maa()
    init_player_and_mnt()

    # 启动 MAA 任务
    maatasker.post_task(entry, _get_override_pipeline()).wait().get()

    mnt.stop()
    logging.debug("Ready to exit")
    sys.exit()


if __name__ == "__main__":
    main()
