import mumuipc
import ldipc


class Player:
    """
    模拟器控制类
    封装了与不同安卓模拟器（MuMu, 雷电）的底层 IPC 通信，主要用于高效获取屏幕截图。
    """
    def __init__(self, type_: str, path: str, index: int) -> None:
        """
        初始化模拟器实例
        :param type_: 模拟器类型，支持 'mumuv4', 'mumuv5', 'ld'
        :param path: 模拟器安装路径或共享内存路径
        :param index: 模拟器多开索引
        """
        self.type = type_
        self.display_id = -1
        if type_ == "mumuv4":
            self.player = mumuipc.MuMuPlayer(path, index, "v4")
        elif type_ == "mumuv5":
            self.player = mumuipc.MuMuPlayer(path, index, "v5")
        elif type_ == "ld":
            self.player = ldipc.LDPlayer(path, index)
        else:
            import logging
            logging.error(f"Unknown player type_: {type_}. self.player will not be initialized.")

    @property
    def resolution(self):
        """
        获取模拟器当前分辨率
        """
        return self.player.resolution

    def ipc_capture_display(self):
        """
        通过 IPC 机制高效捕获模拟器屏幕画面
        :return: 包含屏幕像素数据的 numpy 数组 (RGB 格式)
        """
        if self.type.startswith("mumu"):
            # MuMu 模拟器需要先获取目标应用的 display_id
            if self.display_id == -1:
                self.display_id = self.player.ipc_get_display_id(
                    "com.bilibili.star.bili" # B服包名
                )
            # 捕获画面，并将 RGBA 转换为 RGB
            return self.player.ipc_capture_display(self.display_id)[:, :, :3]
        else:
            # 雷电模拟器直接捕获，返回 RGB
            return self.player.capture()
