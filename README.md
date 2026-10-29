# voice-relay · 五进程语音智能体

一个把「听 / 想 / 说 / 读」拆成四条链路的本地智能体。浏览器只连一个端口，三个异构引擎在它背后。
说一句话它听得懂，问一句它边想边说，想完可以念出来；截图和 PDF 直接丢给它读。

**Python 3.11 · FastAPI · asyncio · Vue 3 + TypeScript · SSE · PyTorch (CPU) · 零数据库**

![架构：五进程三层](docs/architecture.svg)

---

## 这个项目在解决什么

市面上的聊天框已经够多了。这个项目真正想解决的是**四种能力的成本结构完全不同，不该被塞进同一个进程**：

| 能力 | 谁来做 | 判断依据 |
|---|---|---|
| **想** | `deepseek-flash`（远程） | 本地跑不动大模型，也没必要——它是最不敏感的一环 |
| **听** | `Qwen3-ASR-0.6B`（本地 CPU） | 语音是最私密的数据。这条链路**断网也能用**，音频不出本机 |
| **说** | `edge-tts`（远程合成） | 只需要网，不需要显卡。本地跑 TTS 是纯浪费 |
| **读** | 图片直传 + 本地抽取 | 模型原生支持图片，但**不收 PDF** —— 所以文档必须自己抽成文本 |

这四条链路的**延迟特征、失败模式、资源占用**全都不一样。把它们放进一个 FastAPI 应用里，结果是：一个模型加载失败，整个应用起不来；ASR 的一个请求占满事件循环，聊天跟着卡死。

所以拆成了五个进程。

---

## 为什么是五个进程，边界怎么定的

```
浏览器 :5173 ──▶ relay :9000 ──┬──▶ engine/deepseek :8000  想 + 读
                               ├──▶ engine/asr      :8001  听
                               └──▶ engine/tts      :8002  说
```

三个边界，每一个都有具体的理由，不是"微服务更优雅"：

**① 协议层不含业务。** 它只做四件事：分发、分路超时、错误收口、分配 `X-Trace-Id`。它不认识 DeepSeek，也不知道 ASR 跑的是什么模型。

分路超时是这个决定最直接的证据：三条链路的超时策略**必须不同**。

| 路径 | 超时策略 | 为什么 |
|---|---|---|
| 聊天 | 只设连接超时 10s + **块间隔** 300s，不设总时长 | 流式回答可能长达几分钟，设总超时会把正常回答切断 |
| ASR | 总时长 120s | CPU 推理，4 秒音频要 3 秒；用户可能录 60 秒 |
| TTS | 总时长 30s | 远程合成，正常几秒内返回 |

如果把这套策略写进各个业务模块，就会变成三份互不知情的超时配置——加第四路时一定漏。

**② ASR 必须独立成进程，因为它持有进程级资源。** 1.8GB 权重加载一次要 1.5~2 秒。如果按请求加载，每个请求都白等两秒。模型应该是**进程级**资源，不是请求级——这是它不能和别人共处的根本原因，跟"整洁"无关。

**③ TTS 也独立，因为它和聊天是一对反面。** `edge-tts` 和 DeepSeek 同为远程服务，都只是 HTTP 客户端。但把 TTS 塞进聊天服务，会让"说"和"想"耦合：朗读失败不该影响对话，对话超时也不该掐掉正在播的音频。

**代价（必须说清楚）**：五个进程 = 五个端口 = 五个 venv，本地启动要一个统一入口（`dev.py` 就是干这个的），部署时也要多一份编排。换来的是：任一路崩溃不影响另外两路，任一路可以独立重启和调参。

这个交换在"一个人维护的本地应用"这个前提下是划算的。如果是团队协作的对外服务，我会把 8002 并回 relay，把 ASR 换成云 API，回到两个进程。

---

## 一次请求的完整路径

以一次带图片的提问为例，看 `X-Trace-Id` 是怎么把 4 跳串起来的：

```
① 前端  view/src/api/chat.ts
   fetch POST /api/chat/stream，ReadableStream 增量解析 SSE

② 协议层  relay :9000
   生成 trace-id（16 位）→ 写响应头 → 转发下游 → 出错时一并返回
   用 httpx.AsyncClient(trust_env=False) —— 本机有 HTTP_PROXY，不关会劫走 127.0.0.1

③ 模型层  engine/deepseek :8000
   build_messages：注入 system（客户端传的一律丢弃）、截 20 条、过滤空内容
   AsyncOpenAI 流式调用 → 把 chunk 归一成 dict 事件

④ 前端  useChat.ts
   写入响应式数组 → MessageList 增量渲染
```

事件只有四种，前端不需要理解 OpenAI 的返回结构：

| type | 字段 | 说明 |
|---|---|---|
| `reasoning` | `content` | 思维链增量（折叠在回答上方） |
| `delta` | `content` | 正文增量 |
| `done` | `reason` | `stop` 正常 / `length` 被截断 |
| `error` | `message` | 已翻译成人话的错误 |

---

## 六个工程判断

这些是踩过之后改的，每个都有代价：

**1. `max_tokens` 是 16384，不是默认值。**
设 8192 时，带思维链的回答会被**思考过程吃光**——`finish_reason` 返回 `length`，正文一个字都没有，界面上只有一片空白。实测这个 API 支持到 32768，16384 是"够用且不浪费"的折中。

**2. ASR 线程数必须在加载模型前钉死。**
同一条 5.5 秒音频：不显式设线程要 6.33 秒，设成物理核数后 2.28 秒，**差 2.8 倍**。而且 `torch.set_num_threads` 必须在任何并行工作开始前调用。这不是优化项，是必须写死的配置。

**3. 错误必须分级，不能统一报"密钥无效"。**
一开始所有 `APIError` 都说"密钥无效或余额不足"。结果上传一张格式不对的图片（400）也报这个——排查方向完全跑偏。现在 400 单独先捕获，把上游原文带出来；401 才是密钥问题；超时和连接失败各自有文案。

**4. 后端无状态，历史留在前端。**
加数据库就要处理用户、迁移、备份、并发写，成本远大于收益。代价很明确：**换浏览器或清缓存就没了**。所以补了导出 Markdown 作为退路。这是主动取舍，不是省事。

**5. 附件正文有总预算（24000 字）。**
文档和图片的成本结构不同：图片是一次性开销，文档正文**每轮都要重发**。一条 20000 字的 PDF 聊十轮就是 20 万字符的重复用量。所以预算从最新往回分配，更早的附件降级成"文件名 + 已省略"。代价是：回头追问三小时前传的文件，会明显感觉它忘了。

**6. 错误消息不能被喂回模型。**
早期版本把"网络连接失败"这类错误气泡也放进对话历史发给模型——等于告诉它这句话是你说的，会带偏后续回答。现在构造历史时过滤掉 `error` 标记的消息。

---

## 三个数字

| 数字 | 含义 |
|---|---|
| **2.8×** | ASR 线程数钉死前后的速度差（6.33s → 2.28s，5.5 秒音频） |
| **1.40×** | 稳定态实时率：4.2 秒中文音频用 3.01 秒。60 秒录音约需 43 秒 |
| **94 项** | 回归测试（会话层 53 + 播报切段 12 + 文档抽取 29） |

跑一遍（秒级）：

```bash
cd view
npm run test:session    # 会话持久化 / 搜索 / 消息 ID 迁移 / 附件 payload
npm run test:tts        # 播报切段：不丢字、每段 ≤80 字、代码块剔除
npm run typecheck       # tsc --noEmit

cd ../engine/deepseek
venv/Scripts/python.exe _extract_test.py   # PDF/Word/Excel/编码/截断/异常
```

> **这 94 项不包含协议层。** `relay/` 的分路超时、错误映射、`trace` 透传目前一行测试都没有——
> 它是这套工程里最明显的空缺，也是接下来要补的第一件事（见「已知限制与改进路径」）。

---

## 可靠性：四道防线

| 防线 | 做法 | 挡住了什么 |
|---|---|---|
| 分路超时 | 三条链路三套策略（见上表） | 一个慢请求拖垮整站 |
| 错误收口 | 统一错误体 + `X-Trace-Id` | 用户看到的是人话，日志里能串起 4 跳 |
| 异常分级 | APIError / APITimeout / APIConnection 分开处理 | 不再把"格式错"说成"密钥失效" |
| 可中断 | 前端 `AbortController` + `Esc` | 想停就能停，不留悬空连接 |

两个真实踩过的坑，顺便记在这里：

- **uvicorn `reload=True` 在 Windows 会留孤儿子进程**，占着端口导致"改了代码不生效"。协议层和 ASR 已改 `reload=False`，`dev.py` 停止时用 `taskkill /T` 连子进程一起收。
- **Vite 只监听 IPv6 `[::1]`**，所以浏览器必须用 `localhost`，写 `127.0.0.1` 可能打不开。这不是 bug，是默认行为。

---

## 演进：从 1 个进程到 5 个

`backend/` 和 `frontend/` 是第一阶段的版本：单进程 FastAPI + 一个聊天页，只做文字。跑通之后发现三个问题，才有了第二阶段的拆分：

1. 语音识别一进来，模型加载把启动时间从 1 秒拉到十几秒
2. ASR 推理占满 CPU，聊天接口的响应明显变慢
3. 三套依赖（torch / openai / edge-tts）装在一个 venv 里，1.4GB，改一个装半天

两个版本都留在仓库里，**没有删**。原因是它记录了"为什么变成现在这样"——这比只留最终形态更能说明问题。

---

## 接口契约

前端只认一个 baseURL（`/api`），后端全部挂在 relay 上：

| 方法 | 路径 | 转发到 | 说明 |
|---|---|---|---|
| `GET` | `/api/health` | 8000 + 8001 + 8002 | 一次看到所有下游状态 |
| `POST` | `/api/chat/stream` | 8000 | SSE，逐块透传 |
| `POST` | `/api/extract` | 8000 | 上传文件，返回 `{ok, name, kind, text, chars, truncated, pages, scanned, note}` |
| `POST` | `/api/asr` | 8001 | 上传录音，返回 `{text, language, duration}` |
| `POST` | `/api/tts` | 8002 | 返回 `audio/mpeg` |
| `GET` | `/api/balance` | 8000 | 账户余额 |

`content` 有两种形态：纯文本用字符串；带图时展开成 parts 数组。

```jsonc
{"role": "user", "content": "你好"}

{"role": "user", "content": [
  {"type": "text", "text": "这张图错在哪"},
  {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
]}
```

`deepseek-flash` 原生支持图片，但**不支持文档**：它的 `file` 类型只接受 webp/png/jpeg/gif，PDF 和 docx 会被 400 拒绝。所以文档走 `/api/extract` 在本地抽成纯文本，拼进 `content` 再按普通消息发出。

**抽取能力的边界**（诚实列出）：

| 能 | 不能 |
|---|---|
| PDF（文字型，保留阅读顺序） | 扫描版 PDF（抽不出字 → 报错并建议改发截图） |
| Word `.docx`（标题转 Markdown，表格转竖线，段落与表格保持原顺序） | 老格式 `.doc` |
| Excel `.xlsx`（每表一节，只取公式结果） | 老格式 `.xls` |
| CSV / TXT / MD / JSON / 代码（自动识别 UTF-8 / BOM / GBK） | PPT（建议截图） |

单文件 20MB、单次最多 4 个、抽取结果截断到 20000 字。

---

## 本地跑起来

**两个不进 git 的大件**（体积原因）：`engine/asr/bin/ffmpeg.exe`（64MB）和 `engine/asr/weights/Qwen3-ASR-0.6B/`（1.75GB）。下载方式见 [docs/setup.md](docs/setup.md)。

```bash
# 1) 聊天服务（含文档抽取）
cd engine/deepseek
E:/python/python.exe -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env      # 填 DEEPSEEK_API_KEY

# 2) 协议层
cd relay && E:/python/python.exe -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env

# 3) TTS
cd engine/tts && E:/python/python.exe -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt

# 4) ASR —— 顺序不能反
cd engine/asr && E:/python/python.exe -m venv venv
venv/Scripts/python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
venv/Scripts/python.exe -m pip install qwen-asr --no-deps
venv/Scripts/python.exe -m pip install "transformers==4.57.6" librosa soundfile nagisa numpy scipy accelerate qwen-omni-utils fastapi uvicorn python-multipart

# 5) 前端
cd view && npm install
```

> **ASR 安装顺序为什么不能反**：先装 `qwen-asr` 会让 pip 拉 CUDA 版 torch（2.5GB+）；
> `--no-deps` 是绕开它把 `gradio` 拖进来——pip 会为满足 gradio 约束逐个回溯下载历史版本，白下几百 MB。

**启动**：双击 `start.bat`，或

```bash
E:/python/python.exe dev.py            # 五个服务，一个终端，就绪后自动开浏览器
E:/python/python.exe dev.py --check    # 只体检：venv 全不全、端口占没占
```

`dev.py` 按依赖顺序逐个等就绪（ASR 那步会等模型加载完），日志五色前缀混排，`Ctrl+C` 一次全停。
端口已被监听的服务会自动跳过，所以"起全量 + 单独重起一个来调试"不打架。

**顺序**：`8000 / 8001 / 8002` → `9000` → `5173`。先起网关不报错（它不探活），但一收请求就是 502，日志只有"连接被拒绝"，很难查。

逐级验证：

```bash
curl --noproxy '*' 127.0.0.1:8000/api/health   # {"status":"ok","model":"deepseek-flash"}
curl --noproxy '*' 127.0.0.1:8001/health       # loaded: true
curl --noproxy '*' 127.0.0.1:8002/health       # {"engine":"edge-tts"}
curl --noproxy '*' 127.0.0.1:9000/api/health   # upstream_ok: 3/3
```

> 本机有 `HTTP_PROXY`，curl 必须加 `--noproxy '*'`，否则连 `127.0.0.1` 也会被塞进代理、返回 502。

---

## 目录结构

```
relay/                  协议层：唯一入口、分发、分路超时、错误收口、trace id
engine/deepseek/        模型层·想：流式对话 + 文档抽取
engine/asr/             模型层·听：本地 Qwen3-ASR，模型常驻
engine/tts/             模型层·说：edge-tts，按句分段
view/                   前端：Vue3 + TS，SSE 渲染、多会话、语音输入
docs/                   架构图与安装补充
backend/ frontend/      第一阶段版本（单进程，仅作演进记录）
dev.py start.bat        一键启动
```

---

## 如果只读五个文件

6000 多行里，决定这个项目性格的只有五处：

| 文件 | 看什么 |
|---|---|
| `relay/app/routers/chat.py` | 三条链路的超时为什么必须分开设，错误怎么收口成同一结构 |
| `relay/app/upstream.py` | 一个 `trust_env=False` 解决本机 `HTTP_PROXY` 劫持 `127.0.0.1` |
| `engine/asr/app/model_holder.py` | 模型为什么必须是进程级资源，线程数为什么要在加载前钉死 |
| `engine/deepseek/app/services/extract_service.py` | 文档顺序保真、扫描件两档降级、编码探测 |
| `dev.py` | 多进程的启动顺序、就绪探测，以及 `Ctrl+C` 为什么必须连子进程一起杀 |

---

## 已知限制与改进路径

分两类：前面是**有意识接受的取舍**，后面是**确实还没做**。分开写是因为「选择不做」和「没做完」是两件事，混在一起会让人以为前者也是欠账。

**有意识接受**

- **语音识别是两段式的**：录音 → 转文字 → 回填输入框 → 你改错字 → 再发送。不是缺功能：中文 CER 约 2%，错字直接喂给模型会基于错误前提输出一堆废话。 → 做实时听写要换流式模型并在前端做增量对齐，成本远高于收益。
- **历史只存在浏览器**。换设备、清缓存就没了，靠导出 Markdown 兜底。 → 加持久化就要处理用户、迁移、备份，单机场景下是负收益。
- **扫描版 PDF 读不了**（抽不出字），但可以截图发给它——图片那条路能直接看。 → 接本地 OCR 可解，代价是再加一个权重和一份常驻内存。
- **多图只支持并列**，没有做框选局部再问的交互。 → 纯前端工作：画布选区 + 坐标映射到原图。
- **并发能力有限**：ASR 是 CPU 单进程，两个人同时说话就排队。 → 换云 ASR 或加推理队列，但会牺牲「音频不出本机」这个前提。
- **未做鉴权**。当前假定只在本机 `127.0.0.1` 上使用，所有服务都不监听 `0.0.0.0`。 → 对外之前必须先加 token 校验与限流，否则等于把 API key 挂在公网。

**确实还没做**

- **协议层没有自动化测试**。最明显的空缺：分路超时、错误映射、`trace` 透传全是最该被固定住的逻辑，却一行测试都没有；94 项测试全部集中在会话层、播报切段和文档抽取。 → 用 pytest + `httpx.MockTransport` 补契约测试，不需要起真实服务。
- **没有熔断与重试**。上游挂掉直接 5xx，不退避、不做半开探测。 → 单机单用户下重试意义有限，但瞬时失败至少该重试一次。
- **没有 CI**。Gitee Go 需额外开通，改用本地脚本代替。 → `scripts/check.py` 一条命令跑完四套测试和类型检查。
- **没有进程守护**。某个引擎崩了不会自动拉起，要回终端看日志。 → `dev.py` 加健康轮询与重启，或交给 systemd / supervisord。

---

## 关于提交历史

本仓库的提交历史自 2026-10 整理时开始，此前项目在本地从单进程原型演进而来。整理前的实现是完整可跑的，不是脚手架；后续提交记录的是持续改进，不是补作业。

## License

MIT
