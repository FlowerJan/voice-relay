"""后端配置：模型名 / 地址 / Key / System Prompt。

四项核心配置的唯一归属：
- DEEPSEEK_MODEL   模型名称（固定，前端改不动）
- DEEPSEEK_BASE_URL 模型地址（只写域名，SDK 自己拼 /chat/completions）
- DEEPSEEK_API_KEY 模型 Key（仅 .env 提供，代码不留真实值）
- SYSTEM_PROMPT   系统提示词（写死在代码，前端不传也不能传）
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# 系统提示词：唯一来源就是这里，后端每次请求统一插入，忽略客户端传的 system。
SYSTEM_PROMPT = (
    "你是一个中文技术助手，名字叫小智。\n\n"
    "回答规则：\n"
    "1. 用中文回答。直接给结论，不要开场白——不要“好的”“很高兴为你解答”“这是个好问题”这类话。\n"
    "2. 需要分点时用 Markdown 列表；需要对比时用表格；给代码时用带语言标注的代码块。\n"
    "3. 回答长度和问题复杂度匹配。简单问题两三句说清，不要展开；复杂问题才分层展开。\n"
    "4. 不确定的事直接说“不确定”，不要编造 API、参数、链接。\n"
    "5. 不要复述用户的提问。\n"
)


class Settings(BaseSettings):
    # 三项进 .env，一项（模型名）给默认值兜底
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-flash"
    temperature: float = 1.0
    # deepseek-flash 的思维链与正文共用此预算：深问题思维链可达数千 token，
    # 8192 容易在正文开头就被截断，故提高到 16384。
    max_tokens: int = 16384
    history_limit: int = 20  # 送模型的最近对话条数（user/assistant，不含 system）

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
