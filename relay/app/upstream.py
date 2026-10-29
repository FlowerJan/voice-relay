"""下游共享 HTTP 客户端。

每次请求新建 AsyncClient 会反复建/拆连接池（协议层一次对话要打两三次下游），
进程内共用一个，由 main.py 的 lifespan 关闭。
"""
import httpx

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=None),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            # ⚠️ 关键：本机存在 HTTP_PROXY/HTTPS_PROXY 环境变量。
            # 若不关掉 trust_env，127.0.0.1:8000 这类**内部**调用会被塞进外部代理，
            # 表现为 502 / 连接被拒，且报错信息完全看不出原因。
            trust_env=False,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
