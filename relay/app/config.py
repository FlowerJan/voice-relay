"""协议层配置：下游地址 + 分路超时。

协议层只做分发和收口，不含业务。这里定义"下游是谁"和"各路等多久"。

分路超时是这一层存在的核心理由之一：
- ASR 是本地 CPU 推理，慢但**必须**给足（默认 120 秒）
- 聊天是远程流式，可能说十几秒，**不能设总超时**，只设连接超时

共用一个超时值，必然有一个是错的。
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # 下游地址
    chat_base_url: str = "http://127.0.0.1:8000"
    asr_base_url: str = "http://127.0.0.1:8001"
    tts_base_url: str = "http://127.0.0.1:8002"

    # 分路超时
    asr_timeout: float = 120.0        # ASR 总超时（秒）：本地 CPU 推理，必须给足
    extract_timeout: float = 90.0     # 文档解析总超时（秒）：本地 CPU 解析，比 ASR 快一个量级
    chat_connect_timeout: float = 10.0  # 聊天只设连接超时，不设总超时（流式可能很长）
    chat_read_timeout: float = 300.0   # 两次数据块之间的最大间隔（不是总时长）

    # 首块重试：上游在吐出第一个数据块之前失败，可以安全重试一次；
    # 一旦首块到达就绝不再试，否则前端会看到重复内容。
    # 设为 0 即关闭重试。
    chat_retry_times: int = 1
    chat_retry_backoff: float = 0.5

    # 请求体上限（字节）。四条链路都要有，数值不同——上限的意义是"挡住正常用法
    # 永远碰不到的量级"，不是"卡住正常用法"，所以每个值都按该链路的真实体积反推：
    # chat 装的是 base64 图片 + 文档正文，TTS 只有要朗读的文字，
    # ASR 是音频，抽取是 20MB 文件加 multipart 头。
    chat_max_body: int = 16 * 1024 * 1024
    tts_max_body: int = 512 * 1024
    asr_max_body: int = 25 * 1024 * 1024
    extract_max_body: int = 24 * 1024 * 1024

    # 链路 ID 前缀，方便在三个服务的日志里一眼认出是哪一跳
    service_name: str = "relay"

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
