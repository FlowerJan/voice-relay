// SSE 流式请求封装：fetch + ReadableStream 手动解析。
// EventSource 只支持 GET，这里必须 POST + 手动解析 data: 帧。

import { parseErrorBody, readApiError, type ErrorInfo } from './errors'

/**
 * 附件的协议负载：**正文原样上传**，`<document>` 围栏和转义由服务端的
 * `doc_fence.py` 负责。
 *
 * 为什么不在前端拼：转义是协议的一部分，不是界面的行为。前端拼的话，
 * 任何绕过这个 UI 直连 8000 的客户端都能把文档里的 `</document>` 送进去，
 * 凭空截断文档、把后面变成"用户的新指令"。
 */
export interface ChatDocPayload {
  name: string
  /** 「PDF，3 页，内容已截断」这类元信息，只有抽取服务知道，所以这边拼。 */
  meta: string
  /** 空串表示"这次不给全文"，服务端会换成一句省略说明。 */
  body: string
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  /**
   * 图片 dataURL（仅 user 消息会有）。
   * 这里放的是**原图**，不是缩略图——模型需要看清细节。
   */
  images?: string[]
  docs?: ChatDocPayload[]
}

export type ContentPart =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url: { url: string } }

export interface ApiMessage {
  role: string
  content: string | ContentPart[]
  docs?: ChatDocPayload[]
}

/**
 * 把内部消息转成上游格式：只有带图的消息才展开成 parts 数组。
 *
 * 纯文本消息保持字符串形式，不要一律转数组——虽然上游两种都吃，但
 * 字符串是这条链路的常态，转换越多越容易在某处被当成结构错误。
 */
export function toApiMessages(messages: ChatMessage[]): ApiMessage[] {
  return messages.map((m) => {
    const out: ApiMessage = { role: m.role, content: m.content }
    if (m.docs?.length) out.docs = m.docs
    if (m.images?.length) {
      const parts: ContentPart[] = []
      if (m.content.trim()) parts.push({ type: 'text', text: m.content })
      for (const url of m.images) parts.push({ type: 'image_url', image_url: { url } })
      out.content = parts
    }
    return out
  })
}

/** done 事件携带的裁剪统计。条数是"几条消息"，字符数是"那些消息一共多少字"。 */
export interface DoneStats {
  /** 本轮实际发给模型的历史消息条数（不含 system） */
  used: number
  /** 被条数上限或 token 预算裁掉的条数。0 表示一轮没丢 */
  dropped: number
  /**
   * 被裁掉那些消息一共多少字。0 表示"不知道"（老引擎不发这个字段）或没裁。
   *
   * 为什么必须带着它：条数几乎不携带信息。一条三万字文档被整条砍掉也只算 1 条，
   * 用户看到"省略 1 条"会以为丢的是一句寒暄。提示要和真实损失对齐。
   */
  droppedChars: number
}

export interface StreamCallbacks {
  onDelta: (content: string) => void
  onReasoning: (content: string) => void
  /** reason: 'stop' 正常结束；'length' 达到 max_tokens 被截断 */
  onDone: (reason: string, stats: DoneStats) => void
  /**
   * 带 message + hint 的错误对象，不是裸字符串。
   *
   * 裸字符串会逼调用方在这一个回调里丢掉"下一步做什么"——而后端的 hint 是
   * 唯一知道该做什么的地方（它知道 8000 没起来、知道录音超了 120 秒）。
   */
  onError: (err: ErrorInfo) => void
}

/** 流式请求建立之前的失败：这时前端还没收到任何字节，可以整条重来。 */
function networkError(): ErrorInfo {
  return {
    message: '网络连接失败',
    hint: '确认网关（9000）在运行，然后重试',
    status: 0,
  }
}

export async function streamChat(
  messages: ChatMessage[],
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  let resp: Response
  try {
    resp = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: toApiMessages(messages) }),
      signal,
    })
  } catch (e) {
    // 网络层失败（含 AbortError）
    if ((e as Error)?.name === 'AbortError') return
    callbacks.onError(networkError())
    return
  }

  if (!resp.ok) {
    // 以前这里写 `请求失败：${resp.status}`，把后端的 message 整个丢了。
    // 现在把统一错误体读出来：用户看到的是"聊天服务（8000）未启动或无法连接"
    // 以及"确认 8000 端口在跑着"，而不是一个 502。
    callbacks.onError(await readApiError(resp))
    return
  }

  if (!resp.body) {
    callbacks.onError({ message: '响应里没有数据流', status: resp.status, hint: '重试一次' })
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE 帧以空行分隔
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''

    for (const frame of frames) {
      const line = frame.trim()
      if (!line.startsWith('data:')) continue
      const json = line.slice(5).trim()
      if (!json) continue
      try {
        const data = JSON.parse(json)
        if (data.type === 'delta') callbacks.onDelta(data.content)
        else if (data.type === 'reasoning') callbacks.onReasoning(data.content)
        else if (data.type === 'done')
          // 裁剪统计允许缺失（老引擎不发这两个字段），缺失按 0 处理：
          // 宁可少提示一句，也不能因为读不到字段就整轮报错。
          callbacks.onDone(data.reason ?? 'stop', {
            used: Number(data.history_used) || 0,
            dropped: Number(data.history_dropped) || 0,
            droppedChars: Number(data.history_dropped_chars) || 0,
          })
        // 流内错误由引擎发出，形状是 {type:'error', message}（没有 error 包层），
        // 所以先包一层再走同一套解析：两条错误来源最终必须是同一个 ErrorInfo。
        // status 记 0：连接是好的（HTTP 200），出问题的是流里面的这次回答。
        else if (data.type === 'error') callbacks.onError(parseErrorBody({ error: data }, 0))
      } catch {
        // 忽略解析失败的单帧
      }
    }
  }
}
