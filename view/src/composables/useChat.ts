import { ref } from 'vue'
import { streamChat, type ChatDocPayload, type ChatMessage } from '../api/chat'
import { uid } from '../utils/id'
import { getImages } from '../utils/imageStore'
import type { MsgDoc } from '../utils/doc'

/** 界面上的图片：只带缩略图，原图在 IndexedDB 里按 id 取。 */
export interface MsgImage {
  id: string
  thumb: string
  w: number
  h: number
  name: string
}

/**
 * 附件正文的总预算（字符）。
 *
 * 文档和图片不一样：图片是一条固定的 prompt 开销，文档正文是**每轮都要重发的**。
 * 一条 20000 字的 PDF，聊十轮就是 20 万字符的重复用量，费用和延迟都会明显起来。
 * 所以只让最近的附件带全文，更早的降级成"文件名 + 已省略"——
 * 用户回头追问旧文件时会明显感到"它怎么忘了"，但那是刻意取舍，不是程序缺陷。
 */
export const DOC_BUDGET = 24000

/**
 * 领域模型，和给上游的 `ChatMessage` 刻意分开：
 * 这里 `images` 是缩略图对象（要持久化），那边 `images` 是 dataURL（要发给模型）。
 * 混用一个字段会让人以为"存的就是发的"，而这两者在体积上差 10 倍。
 */
export interface Message {
  /**
   * 稳定 ID，供 v-for 的 key 使用。
   *
   * 以前用数组下标当 key，编辑重发（splice + push）后下标整体前移，
   * Vue 会把旧节点复用给新消息：思维链的展开状态、"已复制"高亮、
   * 悬停中的操作条都会串到别的消息上。ID 必须跟消息内容绑定，不能跟位置绑定。
   */
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  images?: MsgImage[]
  /** 附件文档（已抽取的纯文本）。只在发给模型时拼进 content，界面单独渲染成 chip。 */
  docs?: MsgDoc[]
  reasoning?: string
  error?: boolean
  /**
   * 错误发生时的下一步动作（来自后端的 hint）。
   *
   * 为什么不把它合并进 content：content 是"模型说的话"，也会被复制、导出；
   * 建议是系统给的，混在一起会让导出的对话里出现一句"确认 8000 端口在跑着"。
   * 分开存，界面分两行渲染，导出时只导 content。
   */
  errorHint?: string
  truncated?: boolean
  /**
   * 这一轮被裁掉的更早历史条数（来自 done 事件，0/未定义表示没裁）。
   *
   * 为什么要存进消息里而不是临时提示：它是**这一轮回答的事实**——"我当时只看到
   * 这些上下文"。刷新、切会话、导出之后仍然成立，所以跟着消息一起持久化。
   * 只放一个全局 toast，用户重看这段对话时会以为模型当年看到了全部。
   */
  historyDropped?: number
  /**
   * 被裁掉那些历史一共多少字（0/未定义表示不知道或没裁）。
   *
   * 和条数一起存：只看条数会把"丢掉整份三万字文档"读成"丢了一句寒暄"。
   */
  historyDroppedChars?: number
}

/** 统一的消息构造入口：任何新消息都必须带 id，避免某个 push 漏了。 */
export function makeMessage(
  role: Message['role'],
  content: string,
  extra: Omit<Partial<Message>, 'id' | 'role' | 'content'> = {},
): Message {
  return { id: uid('msg'), role, content, ...extra }
}

/**
 * 哪些消息的附件可以带全文。
 *
 * 从**最新往回**分配预算：用户正在问的往往就是刚传的文件，
 * 从旧到新分配会导致"新文件被省略、旧文件占着额度"这种最差结果。
 */
export function docBodyBudget(messages: Array<{ docs?: MsgDoc[] }>): boolean[] {
  const flags = new Array(messages.length).fill(false) as boolean[]
  let used = 0
  for (let i = messages.length - 1; i >= 0; i--) {
    const docs = messages[i].docs
    if (!docs?.length) continue
    const cost = docs.reduce((n, d) => n + (d.text?.length ?? 0), 0)
    if (used + cost <= DOC_BUDGET) {
      flags[i] = true
      used += cost
    }
  }
  return flags
}

/**
 * 把附件转成协议负载。
 *
 * 这里**只做映射：不拼标签、不转义**。围栏和转义属于协议，由服务端
 * `doc_fence.py` 统一负责（理由见 `api/chat.ts` 的 ChatDocPayload 注释）。
 * 曾经这里有一份 `escapeDocText`，问题不在实现，在于它只保护了这一个客户端。
 *
 * `withBody=false` 时 body 传空串，服务端渲染成"正文已省略"。这句提示同样不能
 * 在客户端拼——它一旦变成正文字符串，就可以被文件内容伪造。
 */
export function docPayloads(
  docs: MsgDoc[] | undefined,
  withBody: boolean,
): ChatDocPayload[] | undefined {
  if (!docs?.length) return undefined
  return docs.map((d) => ({
    name: d.name || '未命名文件',
    meta: [
      (d.kind || 'file').toUpperCase(),
      d.pages > 0 ? `${d.pages} 页` : '',
      d.truncated ? '内容已截断' : '',
    ]
      .filter(Boolean)
      .join('，'),
    body: withBody ? (d.text ?? '') : '',
  }))
}

/**
 * 流式对话引擎。
 *
 * 这里**不持有消息数组**：会话数据归 useSessions 管，本 composable 只通过
 * getMessages() 现取。原因是切换会话时数组引用会变，如果在这里缓存一份，
 * 切回来就会往已被丢弃的旧数组里写，界面上看不到任何变化。
 */
export function useChat(getMessages: () => Message[]) {
  const isStreaming = ref(false)
  const thinking = ref(false)
  let abortController: AbortController | null = null

  /** 发请求，把增量写进末尾新建的 assistant 占位。调用前 messages 末尾必须是 user。 */
  async function run(messages: Message[]): Promise<void> {
    // 历史快照必须在放入空的 assistant 占位之前取，否则会把空消息发给模型。
    // 同时剔除错误消息：错误文案不是模型说的话，混进历史会带偏后续回答。
    // 带图的消息要把原图从 IndexedDB 取回来——只发缩略图模型看不清字。
    const withBody = docBodyBudget(messages)
    const history: ChatMessage[] = []
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i]
      if (m.error) continue
      const urls = m.images?.length ? await getImages(m.images.map((x) => x.id)) : []
      const docs = docPayloads(m.docs, withBody[i])
      const content = m.content
      // 只传文件不打字是常态，所以"有没有内容"要把附件算进去
      if (!content.trim() && !urls.length && !docs?.length) continue
      const item: ChatMessage = { role: m.role, content }
      if (urls.length) item.images = urls
      if (docs?.length) item.docs = docs
      history.push(item)
    }

    if (!history.length) return

    const idx = messages.length
    messages.push(makeMessage('assistant', '', { reasoning: '' }))

    isStreaming.value = true
    thinking.value = true
    abortController = new AbortController()

    try {
      await streamChat(
        history,
        {
          onReasoning: (c) => {
            // 必须通过响应式代理（messages[idx]）写入。
            // 直接改原始对象会绕过 Vue 的 set 拦截，渲染依赖不触发，DOM 永远停在首帧。
            const m = messages[idx]
            if (!m) return
            m.reasoning = (m.reasoning ?? '') + c
          },
          onDelta: (c) => {
            const m = messages[idx]
            if (!m) return
            m.content += c
            thinking.value = false
          },
          onDone: (reason, stats) => {
            const m = messages[idx]
            if (!m) return
            if (reason === 'length') m.truncated = true
            // 裁掉了历史就必须说：模型"忘事"的真实原因在这里，
            // 不写进消息的话，用户只能归因成"它变笨了"。
            if (stats.dropped > 0) m.historyDropped = stats.dropped
            if (stats.dropped > 0 && stats.droppedChars > 0) {
              m.historyDroppedChars = stats.droppedChars
            }
          },
          onError: (err) => {
            const m = messages[idx]
            if (!m) return
            m.content = err.message
            m.error = true
            if (err.hint) m.errorHint = err.hint
          },
        },
        abortController.signal,
      )
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') {
        const m = messages[idx]
        if (m) {
          // 走到这里说明流已建立、读到一半断了（fetch 阶段的失败由 onError 处理）
          m.content = '网络连接失败'
          m.errorHint = '确认网关（9000）在运行，然后点「重试」'
          m.error = true
        }
      }
    } finally {
      isStreaming.value = false
      thinking.value = false
      abortController = null
      // 掐断时一个字都没收到（或用户点了停止/切了会话）：把空占位删掉，
      // 否则界面上会留一个永远不动的空气泡。
      const m = messages[idx]
      if (m && !m.content && !m.error) messages.splice(idx, 1)
    }
  }

  /** images/docs 允许只带附件不带文字——"这份合同有什么风险" 本来就常常懒得打字。 */
  async function send(text: string, images: MsgImage[] = [], docs: MsgDoc[] = []): Promise<void> {
    if (isStreaming.value) return
    const t = text.trim()
    if (!t && !images.length && !docs.length) return
    const messages = getMessages()
    const extra: Partial<Message> = {}
    if (images.length) extra.images = images
    if (docs.length) extra.docs = docs
    messages.push(makeMessage('user', t, extra))
    await run(messages)
  }

  /** 重新生成：丢掉末尾的回答再问一次。 */
  async function regenerate(): Promise<void> {
    if (isStreaming.value) return
    const messages = getMessages()
    if (messages.length && messages[messages.length - 1].role === 'assistant') messages.pop()
    if (!messages.length || messages[messages.length - 1].role !== 'user') return
    await run(messages)
  }

  /** 重试某条失败的回答：把它摘掉，用同一条提问重新要一次。 */
  async function retry(index: number): Promise<void> {
    if (isStreaming.value) return
    const messages = getMessages()
    const m = messages[index]
    if (!m || m.role !== 'assistant') return
    messages.splice(index, 1)
    if (!messages.length || messages[messages.length - 1].role !== 'user') return
    await run(messages)
  }

  /** 编辑某条提问并从那里重发：它之后的所有内容作废。原图与附件跟着一起保留。 */
  async function resendFrom(index: number, newText: string): Promise<void> {
    if (isStreaming.value) return
    const messages = getMessages()
    const m = messages[index]
    if (!m || m.role !== 'user') return
    const t = newText.trim()
    if (!t && !m.images?.length && !m.docs?.length) return
    const { images, docs } = m
    messages.splice(index)
    const extra: Partial<Message> = {}
    if (images?.length) extra.images = images
    if (docs?.length) extra.docs = docs
    messages.push(makeMessage('user', t, extra))
    await run(messages)
  }

  function stop(): void {
    abortController?.abort()
  }

  function clear(): void {
    abortController?.abort()
    const messages = getMessages()
    messages.splice(0, messages.length)
  }

  return { isStreaming, thinking, send, regenerate, retry, resendFrom, stop, clear }
}
