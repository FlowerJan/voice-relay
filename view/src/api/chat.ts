// SSE 流式请求封装：fetch + ReadableStream 手动解析。
// EventSource 只支持 GET，这里必须 POST + 手动解析 data: 帧。

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  /**
   * 图片 dataURL（仅 user 消息会有）。
   * 这里放的是**原图**，不是缩略图——模型需要看清细节。
   */
  images?: string[]
}

export type ContentPart =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url: { url: string } }

/**
 * 把内部消息转成上游格式：只有带图的消息才展开成 parts 数组。
 *
 * 纯文本消息保持字符串形式，不要一律转数组——虽然上游两种都吃，但
 * 字符串是这条链路的常态，转换越多越容易在某处被当成结构错误。
 */
export function toApiMessages(
  messages: ChatMessage[],
): Array<{ role: string; content: string | ContentPart[] }> {
  return messages.map((m) => {
    if (!m.images?.length) return { role: m.role, content: m.content }
    const parts: ContentPart[] = []
    if (m.content.trim()) parts.push({ type: 'text', text: m.content })
    for (const url of m.images) parts.push({ type: 'image_url', image_url: { url } })
    return { role: m.role, content: parts }
  })
}

export interface StreamCallbacks {
  onDelta: (content: string) => void
  onReasoning: (content: string) => void
  /** reason: 'stop' 正常结束；'length' 达到 max_tokens 被截断 */
  onDone: (reason: string) => void
  onError: (message: string) => void
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
    callbacks.onError('网络连接失败')
    return
  }

  if (!resp.ok || !resp.body) {
    callbacks.onError(`请求失败：${resp.status}`)
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
        else if (data.type === 'done') callbacks.onDone(data.reason ?? 'stop')
        else if (data.type === 'error') callbacks.onError(data.message)
      } catch {
        // 忽略解析失败的单帧
      }
    }
  }
}
