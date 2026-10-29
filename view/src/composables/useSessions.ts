/**
 * 会话存储层：多会话 + localStorage 持久化。
 *
 * 为什么放在前端而不是后端：这个项目的既定前提就是"后端无状态、历史只存浏览器"，
 * 加了数据库就要处理用户、迁移、备份，成本远大于收益。
 *
 * 三个刻意的设计：
 *
 *  1. **防抖写盘**。流式输出时每来一个 token 都会改动 messages，如果每次都
 *     JSON.stringify 整个会话数组，长回答会明显掉帧。所以攒 600ms 再写，
 *     另外在 beforeunload 时同步 flush 一次，防止关页面前最后一句丢掉。
 *
 *  2. **超限降级而不是报错**。localStorage 只有 5MB，长对话迟早写满。
 *     写不下时自动裁到每个会话最近 40 条再试一次，并给用户一个可解释的提示——
 *     这比抛异常、静默失败或者让整个页面卡死都好。
 *
 *  3. **模块级单例**。状态定义在模块作用域，任何组件 import 拿到的都是同一份，
 *     不需要为这点功能引入 Pinia。
 */
import { computed, ref, watch } from 'vue'
import { uid } from '../utils/id'
import { deleteImages } from '../utils/imageStore'
import type { Message } from './useChat'

export interface Session {
  id: string
  title: string
  createdAt: number
  updatedAt: number
  messages: Message[]
}

const KEY_SESSIONS = 'vta:sessions:v1'
const KEY_CURRENT = 'vta:current:v1'
const SAVE_DEBOUNCE = 600
const TITLE_MAX = 20
const KEEP_ON_QUOTA = 40

const sessions = ref<Session[]>([])
const currentId = ref('')
const storageError = ref('')

let ready = false
let saveTimer: number | undefined

function newSession(title = '新对话'): Session {
  const now = Date.now()
  return { id: uid('s'), title, createdAt: now, updatedAt: now, messages: [] }
}

function isMessageLike(x: unknown): x is Message {
  if (!x || typeof x !== 'object') return false
  const m = x as Record<string, unknown>
  return (
    (m.role === 'user' || m.role === 'assistant' || m.role === 'system') &&
    typeof m.content === 'string'
  )
}

/**
 * 反序列化要假设数据是脏的：用户可能手改过 localStorage，旧版本结构也可能不一样。
 *
 * 其中一件事是**补消息 ID**：加 ID 之前的存档里消息没有 id 字段，
 * 直接 load 进来会让 v-for 退回下标 key，那个"编辑后思维链状态串位"的旧 bug 会复活。
 * 所以迁移放在这里做，一次读完就全部补齐，不需要用户清缓存。
 *
 * 导出是为了让测试能直接喂脏数据进来（模块是单例，测试里没法重复 init）。
 */
export function parseStoredSessions(raw: string | null): Session[] | null {
  if (!raw) return null
  try {
    const data: unknown = JSON.parse(raw)
    if (!Array.isArray(data)) return null
    const out: Session[] = []
    for (const item of data) {
      if (!item || typeof item !== 'object') continue
      const s = item as Record<string, unknown>
      if (typeof s.id !== 'string' || !Array.isArray(s.messages)) continue
      out.push({
        id: s.id,
        title: typeof s.title === 'string' && s.title.trim() ? s.title : '未命名对话',
        createdAt: Number(s.createdAt) || Date.now(),
        updatedAt: Number(s.updatedAt) || Date.now(),
        messages: (s.messages as unknown[]).filter(isMessageLike).map((m) => {
          const o: Message = {
            id: typeof m.id === 'string' && m.id ? m.id : uid('msg'),
            role: m.role,
            content: String(m.content ?? ''),
          }
          if (typeof (m as Message).reasoning === 'string') o.reasoning = (m as Message).reasoning
          if ((m as Message).error) o.error = true
          if ((m as Message).truncated) o.truncated = true
          // 图片只校验 thumb 和 id：原图在 IndexedDB，可能已被清掉，
          // 这时缩略图仍能显示，不至于整条消息作废。
          const imgs = (m as Message).images
          if (Array.isArray(imgs)) {
            const clean = imgs
              .filter((x) => x && typeof x.id === 'string' && typeof x.thumb === 'string')
              .map((x) => ({
                id: x.id,
                thumb: x.thumb,
                w: Number(x.w) || 0,
                h: Number(x.h) || 0,
                name: typeof x.name === 'string' ? x.name : '',
              }))
            if (clean.length) o.images = clean
          }
          // 附件文档：text 是刚需（重新生成、导出都要用），缺了就直接丢这条附件，
          // 不要留一个"有名字但发不出内容"的空壳——那会让模型收到无意义的占位。
          const docList = (m as Message).docs
          if (Array.isArray(docList)) {
            const cleanDocs = docList
              .filter((d) => d && typeof d.text === 'string' && d.text.length > 0)
              .map((d) => ({
                id: typeof d.id === 'string' && d.id ? d.id : uid('doc'),
                name: typeof d.name === 'string' ? d.name : '未命名文件',
                kind: typeof d.kind === 'string' ? d.kind : 'text',
                text: d.text,
                chars: Number(d.chars) || d.text.length,
                truncated: Boolean(d.truncated),
                pages: Number(d.pages) || 0,
                scanned: Boolean(d.scanned),
                note: typeof d.note === 'string' ? d.note : '',
              }))
            if (cleanDocs.length) o.docs = cleanDocs
          }
          return o
        }),
      })
    }
    return out
  } catch {
    return null
  }
}

function flush(): void {
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = undefined
  }
  try {
    localStorage.setItem(KEY_SESSIONS, JSON.stringify(sessions.value))
    localStorage.setItem(KEY_CURRENT, currentId.value)
    if (storageError.value) storageError.value = ''
  } catch {
    try {
      const trimmed = sessions.value.map((s) => ({
        ...s,
        messages: s.messages.slice(-KEEP_ON_QUOTA),
      }))
      localStorage.setItem(KEY_SESSIONS, JSON.stringify(trimmed))
      storageError.value =
        `本地存储接近上限（每个会话的记忆已缩到最近 ${KEEP_ON_QUOTA} 条），` +
        '更早的消息刷新后会消失。建议导出或删除不用的对话。'
    } catch {
      storageError.value = '本地存储已写满，这次对话不会被保存。请导出后删掉一些旧对话。'
    }
  }
}

function scheduleSave(): void {
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = window.setTimeout(flush, SAVE_DEBOUNCE)
}

function init(): void {
  if (ready) return
  ready = true

  let raw: string | null = null
  let savedCurrent = ''
  try {
    raw = localStorage.getItem(KEY_SESSIONS)
    savedCurrent = localStorage.getItem(KEY_CURRENT) ?? ''
  } catch {
    storageError.value = '浏览器禁用了本地存储，这次对话不会保存。'
  }

  const loaded = parseStoredSessions(raw)
  sessions.value = loaded?.length ? loaded : [newSession()]
  currentId.value = sessions.value.some((s) => s.id === savedCurrent)
    ? savedCurrent
    : sessions.value[0].id

  watch(sessions, scheduleSave, { deep: true })
  watch(currentId, scheduleSave)
  window.addEventListener('beforeunload', flush)
}

/** 侧边栏按最近使用排序，但底层数组保持稳定，避免当前会话在列表里乱跳。 */
const ordered = computed(() => [...sessions.value].sort((a, b) => b.updatedAt - a.updatedAt))

const current = computed(() => sessions.value.find((s) => s.id === currentId.value) ?? null)

/** 返回响应式数组本身（不是副本），调用方 push/splice 才能被 watch 到。 */
const currentMessages = computed<Message[]>(() => current.value?.messages ?? [])

function titleFrom(text: string, m?: Message): string {
  const t = text.replace(/\s+/g, ' ').trim()
  if (t) return t.length > TITLE_MAX ? `${t.slice(0, TITLE_MAX)}…` : t
  // 只传了附件没打字：用文件名当标题。否则传三个文件聊完，
  // 侧边栏会出现三个一模一样的"新对话"，根本分不清。
  const doc = m?.docs?.[0]
  if (doc) return titleFrom(doc.name)
  if (m?.images?.length) return '图片对话'
  return '新对话'
}

function create(): Session {
  const s = newSession()
  sessions.value.push(s)
  currentId.value = s.id
  return s
}

function select(id: string): void {
  if (sessions.value.some((s) => s.id === id)) currentId.value = id
}

function rename(id: string, title: string): void {
  const s = sessions.value.find((x) => x.id === id)
  if (!s) return
  const t = title.replace(/\s+/g, ' ').trim().slice(0, 40)
  if (!t) return
  s.title = t
  s.updatedAt = Date.now()
}

function remove(id: string): void {
  const i = sessions.value.findIndex((s) => s.id === id)
  if (i < 0) return
  const gone = sessions.value[i]
  sessions.value.splice(i, 1)
  releaseImages(gone)
  if (sessions.value.length === 0) sessions.value.push(newSession())
  if (currentId.value === id) currentId.value = sessions.value[0].id
}

/** 删会话/清空时顺手回收原图，否则 IndexedDB 会攒下一堆永远读不到的孤儿。 */
function releaseImages(s: Session): void {
  const ids = s.messages.flatMap((m) => m.images?.map((i) => i.id) ?? [])
  if (ids.length) void deleteImages(ids)
}

function clearCurrent(): void {
  const s = current.value
  if (!s) return
  releaseImages(s)
  s.messages.splice(0, s.messages.length)
  s.title = '新对话'
  s.updatedAt = Date.now()
}

/** 每轮对话结束时调用：首条消息决定标题，顺便把会话顶到列表最前。 */
function touch(): void {
  const s = current.value
  if (!s) return
  const firstUser = s.messages.find((m) => m.role === 'user')
  if (firstUser && (s.title === '新对话' || !s.title.trim())) {
    s.title = titleFrom(firstUser.content, firstUser)
  }
  s.updatedAt = Date.now()
}

function toMarkdown(s: Session): string {
  const lines: string[] = [`# ${s.title}`, '', `> 导出时间：${new Date().toLocaleString()}`, '']
  for (const m of s.messages) {
    if (m.role === 'system') continue
    lines.push(m.role === 'user' ? '## 我' : '## 小智', '')
    if (m.reasoning) {
      lines.push('<details><summary>思考过程</summary>', '', m.reasoning, '', '</details>', '')
    }
    // 导出内嵌缩略图（10KB 量级）而不是原图：原图在 IndexedDB 里，
    // 逐个转 base64 会让导出的 md 变成几十 MB，反而没法看。
    for (const img of m.images ?? []) {
      lines.push(`![${img.name || '图片'}](${img.thumb})`, '')
    }
    // 附件只记文件名和规模，不倒全文：正文动辄两万字，塞进来会让导出文件没法读。
    for (const d of m.docs ?? []) {
      const size = d.pages > 0 ? `${d.pages} 页 / ${d.chars} 字` : `${d.chars} 字`
      lines.push(`> 附件：${d.name}（${size}${d.truncated ? '，已截断' : ''}）`, '')
    }
    lines.push(m.content || '（空）', '')
  }
  return lines.join('\n')
}

function download(s: Session): void {
  const blob = new Blob([toMarkdown(s)], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${s.title.replace(/[\\/:*?"<>|]/g, '_') || '对话'}.md`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function exportCurrent(): void {
  const s = current.value
  if (s && s.messages.length) download(s)
}

/* ---------------- 会话搜索 ---------------- */

export interface SearchHit {
  session: Session
  /** 命中在正文而非标题时给一行上下文；命中标题时为空串。 */
  snippet: string
}

const SNIPPET_PAD = 22

function snippetOf(text: string, at: number, len: number): string {
  const start = Math.max(0, at - SNIPPET_PAD)
  const end = Math.min(text.length, at + len + SNIPPET_PAD)
  const body = text.slice(start, end).replace(/\s+/g, ' ').trim()
  return `${start > 0 ? '…' : ''}${body}${end < text.length ? '…' : ''}`
}

/**
 * 在标题和正文里找关键词。
 *
 * 为什么是"全量线性扫"而不是建索引：本地会话是几十个、每个几千字量级，
 * 一次扫描是微秒级，建索引反而要处理增量更新。真到上千会话再换倒排。
 * 命中优先看标题（更符合直觉），标题没中才回正文，并截一行上下文。
 */
export function searchSessions(list: Session[], query: string): SearchHit[] {
  const k = query.trim().toLowerCase()
  if (!k) return list.map((session) => ({ session, snippet: '' }))

  const hits: SearchHit[] = []
  for (const session of list) {
    if (session.title.toLowerCase().includes(k)) {
      hits.push({ session, snippet: '' })
      continue
    }
    for (const m of session.messages) {
      if (m.role === 'system' || !m.content) continue
      const at = m.content.toLowerCase().indexOf(k)
      if (at >= 0) {
        hits.push({ session, snippet: snippetOf(m.content, at, k.length) })
        break
      }
    }
  }
  return hits
}

export function useSessions() {
  init()
  return {
    sessions,
    ordered,
    currentId,
    current,
    currentMessages,
    storageError,
    create,
    select,
    rename,
    remove,
    clearCurrent,
    touch,
    exportCurrent,
    flush,
  }
}
