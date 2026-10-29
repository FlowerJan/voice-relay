"""查询 DeepSeek 账户余额。

官方接口：GET {base_url}/user/balance
请求头：Authorization: Bearer <api_key>
返回：{"is_available": bool,
      "balance_infos": [{"currency": "CNY"|"USD",
                         "total_balance": "110.00",
                         "granted_balance": "10.00",
                         "topped_up_balance": "100.00"}]}

用标准库 urllib，不为这一个 GET 额外引入 HTTP 客户端依赖。
"""
import json
import urllib.error
import urllib.request
from typing import Dict

from app.config import settings


class BalanceError(Exception):
    """余额查询失败。status_code 用于映射成 HTTP 状态码。"""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _get_json(url: str) -> Dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_balance() -> Dict:
    """返回规范化后的余额信息。失败时抛 BalanceError，由 router 转成错误响应。"""
    url = settings.deepseek_base_url.rstrip("/") + "/user/balance"
    try:
        raw = _get_json(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:200]
        if e.code == 401:
            raise BalanceError("API 密钥无效", 401) from e
        raise BalanceError(f"余额查询失败（HTTP {e.code}）：{body}", 502) from e
    except urllib.error.URLError as e:
        raise BalanceError(f"网络连接失败：{e.reason}", 502) from e
    except TimeoutError as e:
        raise BalanceError("余额查询超时", 504) from e
    except json.JSONDecodeError as e:
        raise BalanceError("余额接口返回格式异常", 502) from e

    infos = raw.get("balance_infos") or []
    if not infos:
        raise BalanceError("余额接口未返回余额信息", 502)

    # 人民币优先；没有就取第一个
    info = next((i for i in infos if i.get("currency") == "CNY"), infos[0])
    return {
        "is_available": bool(raw.get("is_available")),
        "currency": info.get("currency", "CNY"),
        "total_balance": info.get("total_balance", "0"),
        "granted_balance": info.get("granted_balance", "0"),
        "topped_up_balance": info.get("topped_up_balance", "0"),
    }
