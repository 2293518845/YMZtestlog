import logging
import requests
from diskcache import Cache
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter


class BestdoriAPI:
    """
    Bestdori API 交互类
    用于从 Bestdori 获取歌曲列表和谱面数据，并提供本地缓存功能以减少网络请求。
    """
    base = "https://bestdori.com/api"
    _logger = logging.getLogger("BestdoriAPI")
    # 使用 diskcache 在本地 'cache' 目录缓存 API 响应
    _cache = Cache("cache")
    _session = requests.Session()
    # 配置 HTTP 请求重试策略，应对网络不稳定或服务器错误
    _adapter = HTTPAdapter(
        max_retries=Retry(
            total=3,  # 总重试次数
            backoff_factor=2,  # 重试间隔时间的退避因子
            status_forcelist=[500, 502, 503, 504],  # 遇到这些状态码时强制重试
            connect=5,
            read=5,
        )
    )
    _session.mount("http://", _adapter)
    _session.mount("https://", _adapter)

    @staticmethod
    def _fetch_and_cache(url, cache_name, expire=None):
        """
        内部方法：获取 URL 数据并缓存
        如果缓存中存在且未过期，则直接返回缓存数据；否则发起请求并更新缓存。
        """
        if cache_ := BestdoriAPI._cache.get(cache_name):
            BestdoriAPI._logger.info(f"Cache hit for {cache_name}")
            return cache_
        else:
            response = BestdoriAPI._session.get(url).json()
            BestdoriAPI._cache.set(cache_name, response, expire=expire)
            BestdoriAPI._logger.info(f"Cache set for {cache_name}")
            return response

    @staticmethod
    def get_song_list():
        """
        获取所有歌曲列表
        缓存过期时间设置为 1 小时 (3600秒)
        """
        url = BestdoriAPI.base + "/songs/all.5.json"
        return BestdoriAPI._fetch_and_cache(url, "allsongs", expire=3600 * 1)

    @staticmethod
    def get_chart(song_id: str, difficulty: str):
        """
        获取指定歌曲和难度的谱面数据
        谱面数据通常不会改变，因此不设置过期时间（永久缓存）
        """
        cacheid = f"{song_id}-{difficulty}"
        url = BestdoriAPI.base + f"/charts/{song_id}/{difficulty}.json"
        return BestdoriAPI._fetch_and_cache(url, cacheid)


if __name__ == "__main__":
    # 简单的测试代码
    logging.basicConfig(level=logging.DEBUG)
    songlist = BestdoriAPI.get_song_list()
    chart = BestdoriAPI.get_chart(1, "easy")
    pass
