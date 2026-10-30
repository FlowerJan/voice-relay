// SSE 流式请求封装：fetch + ReadableStream 手动解析。
// EventSource 只支持 GET，这里必须 POST + 手动解析 data: 帧。

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
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
      body: JSON.stringify({ messages }),
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
