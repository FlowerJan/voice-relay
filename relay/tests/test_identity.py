"""relay 的浅探针 `/api/identity`。

这个端点只有一个职责：回答"9000 端口上是不是 relay"，**不回答"下游好不好"**。
两件事被混在一起过一次，代价是启动时把"自己在忙"误诊成"外人占位"
（详见 `app/routers/health.py` 的头注释）。所以这里钉住两点：

1. 形状：`{"service": "relay"}`，和 `/api/health` 里那个字段同值同拼法，
   让 dev.py 的判据只有一套；
2. **不碰下游**：哪怕三个下游全挂、哪怕全都很慢，它也必须立刻答上来。
"""
import httpx
import pytest

from app.main import app

pytestmark = pytest.mark.anyio


async def test_identity_is_shallow_and_says_relay():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay.test") as client:
        response = await client.get("/api/identity")

    assert response.status_code == 200
    assert response.json() == {"service": "relay"}


async def test_identity_never_touches_downstream(upstream):
    """把上游设成"一碰就炸"，再探身份。

    如果这个端点哪天被改回聚合探针，这条测试会以 `AssertionError: 测试没有设定
    上游该怎么回` 变红——那正是要它红的时候：身份探针一旦依赖下游，下游忙的时候
    就会被误判成"端口被别的程序占了"。
    """

    def boom(request):
        raise AssertionError(f"身份探针不该请求下游：{request.url}")

    upstream.set(boom)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay.test") as client:
        response = await client.get("/api/identity")

    assert response.status_code == 200
    assert upstream.count == 0, f"身份探针发了 {upstream.count} 个下游请求"
