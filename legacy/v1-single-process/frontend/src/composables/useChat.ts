import { ref } from 'vue'
import { streamChat, type ChatMessage } from '../api/chat'

export interface Message extends ChatMessage {
  reasoning?: string
  error?: boolean
  truncated?: boolean
}

export function useChat() {
  const messages = ref<Message[]>([])
  const isStreaming = ref(false)
  const thinking = ref(false)
  let abortController: AbortController | null = null

  async function send(text: string) {
    if (isStreaming.value) return
    messages.value.push({ role: 'user', content: text })

    // 历史快照必须在放入空的 assistant 占位之前取，否则会把空消息发给模型
    const history: ChatMessage[] = messages.value.map((m) => ({
      role: m.role,
      content: m.content,
    }))

    const idx = messages.value.length
    messages.value.push({ role: 'assistant', content: '', reasoning: '' })

    isStreaming.value = true
    thinking.value = true
    abortController = new AbortController()

    try {
      await streamChat(
        history,
        {
          onReasoning: (c) => {
            // 必须通过响应式代理（messages.value[idx]）写入。
            // 直接改原始对象会绕过 Vue 的 set 拦截，渲染依赖不触发，DOM 永远停在首帧。
            const m = messages.value[idx]
            m.reasoning = (m.reasoning ?? '') + c
          },
          onDelta: (c) => {
            const m = messages.value[idx]
            m.content += c
            thinking.value = false
          },
          onDone: (reason) => {
            if (reason === 'length') {
              messages.value[idx].truncated = true
            }
          },
          onError: (msg) => {
            const m = messages.value[idx]
            m.content = msg
            m.error = true
          },
        },
        abortController.signal,
      )
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') {
        const m = messages.value[idx]
        m.content = '网络连接失败'
        m.error = true
      }
    } finally {
      isStreaming.value = false
      thinking.value = false
      abortController = null
    }
  }

  function stop() {
    abortController?.abort()
  }

  function clear() {
    abortController?.abort()
    messages.value = []
  }

  return { messages, isStreaming, thinking, send, stop, clear }
}
