"""SSE 事件类型的唯一定义处。

为什么单独一个模块：事件类型是**跨进程契约**——引擎发、relay 透传、前端收。
散在 f-string 里写死，改一个名字不会有任何报错，只会让前端静默丢掉那条事件
（`api/chat.ts` 是一串 `else if`，没有兜底分支）。

这里的常量必须与 `contracts/chat-events.json` 完全一致，
由 `relay/tests/test_contract.py` 断言，不一致直接测试失败。

本模块刻意不 import 任何东西，这样别的 venv 也能直接按路径加载它做校验。
"""

REASONING = "reasoning"
DELTA = "delta"
DONE = "done"
ERROR = "error"

#: 引擎可能发出的全部类型
ALL_EVENTS = (REASONING, DELTA, DONE, ERROR)
