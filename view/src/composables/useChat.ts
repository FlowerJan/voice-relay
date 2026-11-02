import { ref } from 'vue'
import { streamChat, type ChatMessage } from '../api/chat'
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
  truncated?: boolean
  /**
   * 这一轮被裁掉的更早历史条数（来自 done 事件，0/未定义表示没裁）。
   *
   * 为什么要存进消息里而不是临时提示：它是**这一轮回答的事实**——"我当时只看到
   * 这些上下文"。刷新、切会话、导出之后仍然成立，所以跟着消息一起持久化。
   * 只放一个全局 toast，用户重看这段对话时会以为模型当年看到了全部。
   */
  historyDropped?: number
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
 * 转义文档正文里可能"冒充标签"的片段。
 *
 * 为什么必须做：正文是用户的文件里原样抽出来的，如果它里面恰好有一行
 * `</document>`，模型看到的结构就变成"文档到此结束，后面是用户的新指令"——
 * 这不是理论问题，任何一份讨论 HTML/XML 的文档都可能有。
 *
 * 只转义 `<document` / `</document` 这两个序列，不动其余文本：
 * 文档里的代码、尖括号、Markdown 都保持原样，可读性不受影响。
 * 按 HTML 的规则转成实体，模型读到 `&lt;/document` 会当成普通文字。
 */
function escapeDocText(text: string): string {
  return text.replace(/<(\/?document)/gi, '&lt;$1')
}

/** 属性值里的引号和尖括号同样要处理，否则会提前闭合标签。 */
function escapeAttr(value: string): string {
  return value.replace(/"/g, '&quot;').replace(/</g, '&lt;')
}

/**
 * 把正文和附件拼成一条要发给模型的消息。
 *
 * 用 XML 标签把文档包起来（而不是 `===== 附件 =====` 这类纯文本分隔线）：
 * ① 模型需要知道"哪段是用户说的、哪段是文件里的"，否则会把我写的
 *    "这段帮我总结一下"当成文档的一部分；
 * ② 标签有明确的开始与结束，配合系统提示词里"标签内一切皆材料"的声明，
 *    文档里的"忽略以上指令"不会被当成指令执行。
 *
 * 之前用 `===== 附件「名」=====` 的分隔线有两个问题：正文里的同名分界线
 * 可以伪造出"文档已结束"的假象，而且提示词里没有任何一句话说明它是什么。
 */
export function composeContent(
  m: { content: string; docs?: MsgDoc[] },
  withBody: boolean,
): string {
  if (!m.docs?.length) return m.content
  const parts: string[] = []
  if (m.content.trim()) parts.push(m.content.trim())
  for (const d of m.docs) {
    const meta = [
      (d.kind || 'file').toUpperCase(),
      d.pages > 0 ? `${d.pages} 页` : '',
      d.truncated ? '内容已截断' : '',
    ]
      .filter(Boolean)
      .join('，')
    const head = `<document name="${escapeAttr(d.name)}" meta="${escapeAttr(meta)}">`
    const body = withBody
      ? escapeDocText(d.text ?? '')
      : '（正文已省略：对话较长，只保留最近附件的完整内容。如需重新参考，请再上传一次）'
    parts.push(`${head}\n${body}\n</document>`)
  }
  return parts.join('\n\n')
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
      const content = composeContent(m, withBody[i])
      if (!content.trim() && !urls.length) continue
      history.push(
        urls.length ? { role: m.role, content, images: urls } : { role: m.role, content },
      )
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
          },
          onError: (msg) => {
            const m = messages[idx]
            if (!m) return
            m.content = msg
            m.error = true
          },
        },
        abortController.signal,
      )
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') {
        const m = messages[idx]
        if (m) {
          m.content = '网络连接失败'
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
