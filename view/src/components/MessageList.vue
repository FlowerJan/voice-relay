<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { renderMarkdown } from '../utils/markdown'
import { getImage } from '../utils/imageStore'
import { docMeta } from '../utils/doc'
import type { Message, MsgImage } from '../composables/useChat'

const props = defineProps<{
  messages: Message[]
  thinking: boolean
  isStreaming: boolean
}>()

const emit = defineEmits<{
  (e: 'regenerate'): void
  (e: 'retry', index: number): void
  (e: 'resend', index: number, text: string): void
}>()

const listRef = ref<HTMLElement | null>(null)
const lastIdx = computed(() => props.messages.length - 1)

const editingIndex = ref(-1)
const draft = ref('')
/** 复制高亮按消息 ID 记，不能按下标：重发后下标会前移，高亮会跳到别的消息上。 */
const copiedId = ref('')
let copyTimer: number | undefined

/**
 * 滚动跟随：只有用户本来就在底部时才跟着新内容走。
 * 否则长回答流式输出时，用户往上翻看前面几句，每来一个 token 就被拽回底部。
 */
const stick = ref(true)
const showJump = ref(false)

function nearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function onScroll() {
  const el = listRef.value
  if (!el) return
  stick.value = nearBottom(el)
  showJump.value = el.scrollHeight - el.scrollTop - el.clientHeight > 200
}

function scrollToBottom(smooth = false) {
  nextTick(() => {
    const el = listRef.value
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
    stick.value = true
    showJump.value = false
  })
}

watch(
  () => props.messages.map((m) => m.content + '|' + (m.reasoning ?? '')).join('@'),
  () => {
    if (stick.value) scrollToBottom()
    else showJump.value = true
  },
)

watch(
  () => props.isStreaming,
  (v) => {
    if (!v) nextTick(decorateCodeBlocks)
  },
)

onMounted(() => {
  scrollToBottom()
  nextTick(decorateCodeBlocks)
})

/** 给渲染出来的代码块补一个复制按钮。流式期间不注入，避免每帧重建 DOM。 */
function decorateCodeBlocks() {
  const root = listRef.value
  if (!root) return
  root.querySelectorAll<HTMLElement>('.content pre').forEach((pre) => {
    if (pre.querySelector('.code-copy')) return
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'code-copy'
    btn.textContent = '复制'
    btn.addEventListener('click', (ev) => {
      ev.preventDefault()
      ev.stopPropagation()
      const code = pre.querySelector('code')?.textContent ?? pre.textContent ?? ''
      void copyText(code).then(() => {
        btn.textContent = '已复制'
        window.setTimeout(() => {
          btn.textContent = '复制'
        }, 1500)
      })
    })
    pre.appendChild(btn)
  })
}

function startEdit(i: number, content: string) {
  if (props.isStreaming) return
  editingIndex.value = i
  draft.value = content
  nextTick(() => {
    // 同理：这个 textarea 在 v-for 里，用 ref="editRef" 会拿到数组而不是元素。
    // 同一时刻只有一条消息处于编辑态，从列表根节点查即可。
    const el = listRef.value?.querySelector<HTMLTextAreaElement>('.edit-input')
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  })
}

function cancelEdit() {
  editingIndex.value = -1
  draft.value = ''
}

function saveEdit() {
  const i = editingIndex.value
  const t = draft.value.trim()
  if (i < 0 || !t) return
  editingIndex.value = -1
  draft.value = ''
  emit('resend', i, t)
}

function autoGrow(e: Event) {
  const el = e.target as HTMLTextAreaElement
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}

/** 复制文本：优先 Clipboard API，非 https 环境下退回 execCommand。 */
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    return
  } catch {
    /* 落到降级分支 */
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  try {
    document.execCommand('copy')
  } catch {
    /* 复制失败也不该打断对话，用户可以手动选 */
  }
  ta.remove()
}

async function copy(text: string, id: string) {
  await copyText(text)
  copiedId.value = id
  if (copyTimer) clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => {
    if (copiedId.value === id) copiedId.value = ''
  }, 1500)
}

const isLast = (i: number) => i === lastIdx.value

/** 点缩略图看原图。原图在 IndexedDB，读不到时退回缩略图，至少不是空白。 */
const zoomSrc = ref('')

async function openZoom(img: MsgImage) {
  zoomSrc.value = img.thumb
  const full = await getImage(img.id)
  if (full) zoomSrc.value = full
}
</script>

<template>
  <div class="list-wrap">
    <div class="message-list" ref="listRef" @scroll.passive="onScroll">
    <div v-if="messages.length === 0" class="empty">
      还没有对话，下面输入一句话开始吧。
    </div>

    <div v-for="(m, i) in messages" :key="m.id" class="row" :class="m.role">
      <div class="avatar">{{ m.role === 'user' ? '我' : '智' }}</div>

      <div class="bubble" :class="{ user: m.role === 'user', error: m.error, editing: editingIndex === i }">
        <div v-if="editingIndex === i" class="edit-box">
          <textarea
            v-model="draft"
            class="edit-input"
            rows="2"
            @input="autoGrow"
            @keydown.esc.prevent="cancelEdit"
            @keydown.enter.exact.prevent="saveEdit"
          ></textarea>
          <div class="edit-ops">
            <button class="mini-btn primary" :disabled="!draft.trim()" @click="saveEdit">
              保存并重发
            </button>
            <button class="mini-btn" @click="cancelEdit">取消</button>
            <span class="edit-hint">Enter 保存 · Esc 取消</span>
          </div>
        </div>

        <template v-else>
          <div v-if="m.images?.length" class="msg-images">
            <button
              v-for="img in m.images"
              :key="img.id"
              type="button"
              class="msg-img-btn"
              :title="`查看原图（${img.name || '图片'}）`"
              @click="openZoom(img)"
            >
              <img :src="img.thumb" :alt="img.name || '图片'" class="msg-img" loading="lazy" />
            </button>
          </div>

          <div v-if="m.docs?.length" class="msg-docs">
            <div
              v-for="d in m.docs"
              :key="d.id"
              class="msg-doc"
              :class="{ warn: d.scanned || d.truncated }"
              :title="d.note || d.name"
            >
              <svg class="msg-doc-ico" viewBox="0 0 24 24" aria-hidden="true">
                <path
                  d="M7 3.5h6.5l4.5 4.5v12.5a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1z"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.6"
                  stroke-linejoin="round"
                />
                <path d="M13.5 3.5V8H18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
              </svg>
              <span class="msg-doc-name">{{ d.name }}</span>
              <span class="msg-doc-meta">{{ docMeta(d) }}</span>
            </div>
          </div>

          <template v-if="m.role === 'assistant' && m.reasoning">
            <details class="reasoning">
              <summary>思考过程</summary>
              <div class="reasoning-body" v-html="renderMarkdown(m.reasoning)"></div>
            </details>
          </template>

          <div v-if="m.error" class="content error-text">{{ m.content }}</div>
          <div v-else-if="m.content" class="content" v-html="renderMarkdown(m.content)"></div>
          <div v-else-if="thinking && i === lastIdx" class="thinking">
            <span class="dot"></span><span class="dot"></span><span class="dot"></span>
            思考中
          </div>

          <div v-if="m.truncated" class="truncated">
            回答达到长度上限被截断，以上内容不完整
          </div>

          <div v-if="m.content || m.error || m.images?.length || m.docs?.length" class="msg-ops">
            <template v-if="m.role === 'user'">
              <button class="op" :disabled="isStreaming" @click="startEdit(i, m.content)">编辑</button>
              <button class="op" @click="copy(m.content, m.id)">
                {{ copiedId === m.id ? '已复制' : '复制' }}
              </button>
            </template>

            <template v-else>
              <button
                v-if="m.error"
                class="op strong"
                @click="emit('retry', i)"
              >
                重试
              </button>
              <button
                v-else-if="isLast(i)"
                class="op"
                :disabled="isStreaming"
                @click="emit('regenerate')"
              >
                重新生成
              </button>
              <button v-if="m.content" class="op" @click="copy(m.content, m.id)">
                {{ copiedId === m.id ? '已复制' : '复制' }}
              </button>
            </template>
          </div>
        </template>
      </div>
    </div>
    </div>

    <button v-if="showJump" class="jump-btn" type="button" @click="scrollToBottom(true)">
      回到底部
    </button>

    <div v-if="zoomSrc" class="zoom-mask" @click="zoomSrc = ''">
      <img :src="zoomSrc" class="zoom-img" alt="图片预览" />
    </div>
  </div>
</template>

<style scoped>
.list-wrap {
  flex: 1 1 auto;
  min-height: 0;
  position: relative;
  display: flex;
  flex-direction: column;
}

.message-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.jump-btn {
  position: absolute;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  border: 1px solid var(--border);
  background: var(--card);
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 6px 14px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  box-shadow: 0 2px 10px rgb(0 0 0 / 12%);
}

.jump-btn:hover {
  color: var(--accent);
  border-color: var(--accent);
}

.empty {
  margin: auto;
  color: var(--text-secondary);
  font-size: 14px;
}

.row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.row.user {
  flex-direction: row-reverse;
}

.avatar {
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: #fff;
  background: var(--accent);
}

.row.user .avatar {
  background: #94a3b8;
}

.bubble {
  max-width: 78%;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 10px 14px;
  word-break: break-word;
}

.bubble.user {
  background: var(--user-bubble);
  color: #fff;
  border-color: var(--user-bubble);
}

.bubble.error {
  border-color: #fecaca;
  background: #fef2f2;
}

.bubble.editing {
  width: 78%;
  border-color: var(--accent);
}

.error-text {
  color: #dc2626;
}

/* ---- 消息里的图片 ---- */
.msg-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

/* ---- 消息里的附件 ---- */
.msg-docs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.msg-doc {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 5px 9px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--bg);
  font-size: 12px;
  color: var(--text-secondary);
}

/* 内容不完整时要能一眼看出来，否则用户会以为模型读到的是全文 */
.msg-doc.warn {
  border-color: #f0b429;
  color: #92700a;
}

.msg-doc-ico {
  flex: none;
  width: 15px;
  height: 15px;
  color: var(--accent);
}

.msg-doc.warn .msg-doc-ico {
  color: #b8860b;
}

.msg-doc-name {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}

.msg-doc-meta {
  flex: none;
  font-size: 11px;
  opacity: 0.85;
}

.msg-img-btn {
  padding: 0;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  background: none;
  cursor: zoom-in;
  line-height: 0;
}

.msg-img {
  display: block;
  max-width: 220px;
  max-height: 160px;
  object-fit: cover;
}

/* 单独一条只有图片的消息，不要留出底下那条细缝 */
.bubble > .msg-images:last-child {
  margin-bottom: 0;
}

.zoom-mask {
  position: absolute;
  inset: 0;
  z-index: 40;
  background: rgb(0 0 0 / 72%);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  cursor: zoom-out;
}

.zoom-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: 6px;
  background: #fff;
}

.truncated {
  margin-top: 8px;
  padding: 6px 10px;
  border-radius: 6px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  color: #92400e;
  font-size: 12px;
}

/* ---- 消息操作条：hover 才出现，触屏设备常驻 ---- */
.msg-ops {
  display: flex;
  gap: 4px;
  margin-top: 6px;
  opacity: 0;
  transition: opacity 0.12s;
}

.bubble:hover .msg-ops,
.bubble:focus-within .msg-ops {
  opacity: 1;
}

@media (hover: none) {
  .msg-ops {
    opacity: 1;
  }
}

.op {
  border: 1px solid transparent;
  background: none;
  border-radius: 6px;
  padding: 2px 8px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  color: var(--text-secondary);
}

.bubble.user .op {
  color: rgb(255 255 255 / 78%);
}

.op:hover:not(:disabled) {
  background: var(--border);
  color: var(--text);
}

.bubble.user .op:hover:not(:disabled) {
  background: rgb(255 255 255 / 18%);
  color: #fff;
}

.op:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.op.strong {
  color: #dc2626;
  border-color: #fecaca;
}

/* ---- 内联编辑 ---- */
.edit-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.bubble.user .edit-box {
  color: var(--text);
}

.edit-input {
  width: 100%;
  resize: none;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font: inherit;
  font-size: 14px;
  line-height: 1.55;
  color: var(--text);
  background: var(--card);
  outline: none;
  min-height: 62px;
}

.edit-input:focus {
  border-color: var(--accent);
}

.edit-ops {
  display: flex;
  align-items: center;
  gap: 8px;
}

.mini-btn {
  border: 1px solid var(--border);
  background: var(--card);
  color: var(--text);
  border-radius: 7px;
  padding: 4px 10px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.mini-btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

.mini-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.edit-hint {
  font-size: 11px;
  color: var(--text-secondary);
}

/* 思维链折叠：上方、默认收起、灰色弱样式 */
.reasoning {
  margin-bottom: 8px;
  border-left: 3px solid var(--border);
  padding-left: 10px;
}

.reasoning summary {
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 12px;
  user-select: none;
}

.reasoning-body {
  margin-top: 6px;
  color: var(--text-secondary);
  font-size: 13px;
}

.thinking {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text-secondary);
  font-size: 13px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-secondary);
  animation: blink 1.2s infinite both;
}
.dot:nth-child(2) {
  animation-delay: 0.2s;
}
.dot:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes blink {
  0%,
  80%,
  100% {
    opacity: 0.2;
  }
  40% {
    opacity: 1;
  }
}

/* Markdown 内容基础排版 */
.content :deep(pre) {
  position: relative;
}

.content :deep(pre.hljs) {
  background: var(--code-bg);
  border-radius: 8px;
  padding: 12px;
  overflow-x: auto;
  margin: 8px 0;
}

.content :deep(.code-copy) {
  position: absolute;
  top: 6px;
  right: 6px;
  padding: 2px 8px;
  font-family: inherit;
  font-size: 11px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--card);
  color: var(--text-secondary);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s;
}

.content :deep(pre:hover .code-copy),
.content :deep(.code-copy:focus) {
  opacity: 1;
}

@media (hover: none) {
  .content :deep(.code-copy) {
    opacity: 1;
  }
}

.content :deep(code) {
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 13px;
}

.content :deep(p) {
  margin: 0 0 8px;
}

.content :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
}

.content :deep(th),
.content :deep(td) {
  border: 1px solid var(--border);
  padding: 6px 10px;
}

.content :deep(a) {
  color: var(--accent);
}

@media (max-width: 768px) {
  .message-list {
    padding: 12px;
    gap: 12px;
  }

  .bubble {
    max-width: 88%;
  }

  .bubble.editing {
    width: 88%;
  }
}
</style>
