# legacy：第一阶段（单进程版）

这是项目最初的样子：一个 FastAPI 进程（`backend/`，监听 8000）加一个 Vite 前端
（`frontend/`，监听 5173）。模型调用、语音、文档抽取要么全挤在同一个进程里，
要么还没有。

**留在这里不是因为它好，而是因为它记录了"为什么要拆"。**
现在是五进程架构（见根目录 README 的架构图），每一条拆分理由都能在这一版里找到出处：

| 这一版的实际问题 | 现在的位置 |
|---|---|
| 一个 1.8GB 的 ASR 模型和聊天请求共用一个事件循环，识别时聊天会卡住 | `engine/asr` 独立进程 |
| 换 TTS 供应商要重启整个后端 | `engine/tts` 独立进程 |
| 前端只认死一个地址，加一路能力就要改前端的 baseURL | `relay` 统一入口 |
| 超时只能设一个值：ASR 需要的 120 秒和聊天流式长回答需要的"无总时长"互相打架 | `relay` 分路超时 |
| 错误格式各处不同，前端要为每种失败写一套处理 | `relay` 错误收口 |

**这一版不是废弃代码，是"为什么改"的证据。** 所以保留，但不维护：
依赖版本、接口路径、提示词内容都可能与现在不同。

## 跑起来

运行环境（`venv/`、`node_modules/`）没有进仓库——它们不属于代码。
要跑的话按 `backend/requirements.txt` 和 `frontend/package.json` 自行安装：

```bash
cd backend
python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env        # 填入 DEEPSEEK_API_KEY
venv/Scripts/python.exe -m uvicorn app.main:app --port 8000

cd ../frontend
npm install
npm run dev                 # 5173，vite 代理 /api 到 8000
```

## 一处需要说明的地方

这一版有账户余额显示（`backend/app/services/balance_service.py` +
`frontend/src/components/BalanceBadge.vue`）。**现在这一版也还有**——
`view/src/components/BalanceBadge.vue` 与 `relay/app/routers/chat.py` 的
`GET /api/balance`，只是搬了位置、多绕了一层协议层（Key 始终只在引擎那侧）。

写在这里，是因为"功能在两版之间悄悄丢了"是最容易被追问的一类问题。
把"它还在、在哪"写清楚，比让人去翻代码找要稳妥。
