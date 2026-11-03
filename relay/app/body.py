"""请求体上限：在读进内存**之前**就把过大的请求拦掉。

`await request.body()` 是一条危险语句——它把整个请求体读进内存，而且没有上限。
协议层是唯一对外端口，这条语句的暴露面正好在最外层：一个 2GB 的 body 打过来，
结果不是"接口变慢"，是**进程没了**。而进程死了，连一条错误日志都不会留下。

两道校验，各挡一种情况，缺一不可：

- **Content-Length 预检** 挡"老实但巨大"的请求。只做"读完再量长度"是没有意义的：
  内存正是在读完那一刻被占掉的。
- **流式累计校验** 挡"Content-Length 缺失或偏小"的请求。分块传输本来就没有这个头，
  谎报也只需要改一行。

上限按真实用法反推，不拍脑袋：chat 的 body 里主要是 base64 图片（6 张 1400px
约 2.2MB）和文档正文，正常请求不到 4MB，16MB 留了四倍余量；TTS 只收要朗读的
文字，512KB 已经是几万字。

**这个上限约束的是"读入"，不是"整条请求的峰值"。** 读出 16MB 之后还要 `json.loads`
再交给 Pydantic：bytes 一份、解析出来的字符串一份、解码后的图片又一份，合起来大约
是 limit 的 2~3 倍（这个倍数是估的，没有实测过）。要压总内存得压 limit，光看这
一个数会低估。这条写在这里，是因为"上限 16MB"很容易被读成"峰值 16MB"。
"""
from fastapi import Request


def limit_text(limit: int) -> str:
    """把字节数说成人话，用在错误文案里。"""
    if limit >= 1024 * 1024:
        return f"{limit // (1024 * 1024)}MB"
    return f"{limit // 1024}KB"


class BodyTooLarge(Exception):
    """请求体超过上限。由路由捕获后转成 413。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.message = f"请求体超过 {limit_text(limit)} 上限"
        super().__init__(self.message)


def declared_too_large(request: Request, limit: int) -> bool:
    """只看声明长度，不读 body。给"边收边转"的两条链路用。

    ASR 和抽取是流式转发（`content=request.stream()`），本来就不在协议层攒内存，
    所以这里只需要在转发**开始之前**拒绝明显超限的请求，不必自己数一遍。

    头缺失或不是纯数字时不拦：那不是"超限"，是"没说"，交给下游自己的上限。
    """
    declared = request.headers.get("content-length")
    return bool(declared and declared.isdigit() and int(declared) > limit)


async def read_body(request: Request, limit: int) -> bytes:
    """读请求体，超限抛 `BodyTooLarge`。给必须拿到完整 body 的两条链路用。

    走 `request.stream()` 而不是 `request.body()`：后者是"读完再返回"，
    前者能在**读到一半**就发现超限并停下，内存峰值不超过一个 chunk 加 limit。

    注意这个承诺只覆盖"读"这一段：读出来的 bytes 之后还要解析成对象，整条请求的
    峰值更高，见文件头那段说明。
    """
    if declared_too_large(request, limit):
        raise BodyTooLarge(limit)

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            # 这里不必把已读的部分留在内存里——异常一抛，局部变量随栈释放。
            raise BodyTooLarge(limit)
        chunks.append(chunk)
    return b"".join(chunks)
