<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useRecorder, MAX_SECONDS } from '../composables/useRecorder'
import type { MsgImage } from '../composables/useChat'
import { ACCEPT, loadImageFile, pickImageFiles } from '../utils/image'
import { putImage } from '../utils/imageStore'
import {
  DOC_ACCEPT,
  MAX_DOCS,
  docMeta,
  extractDoc,
  pickDocFiles,
  type MsgDoc,
} from '../utils/doc'

const props = defineProps<{
  isStreaming: boolean
}>()

const emit = defineEmits<{
  (e: 'send', text: string, images: MsgImage[], docs: MsgDoc[]): void
  (e: 'stop'): void
  (e: 'clear'): void
}>()

const text = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const docInput = ref<HTMLInputElement | null>(null)

/** 待发送的图片。只放缩略图，原图已进 IndexedDB。 */
const pending = ref<MsgImage[]>([])
/** 待发送的文档。text 就是全文，随消息一起存。 */
const pendingDocs = ref<MsgDoc[]>([])
const attachError = ref('')
const loading = ref(0)
/** 正在解析的文档数：解析比压图片慢得多（PDF 十几秒），必须让用户看到"在动"。 */
const docBusy = ref(0)
const MAX_IMAGES = 6

const rec = useRecorder()

async function addFiles(files: File[]) {
  if (!files.length) return
  attachError.value = ''
  const room = MAX_IMAGES - pending.value.length
  if (room <= 0) {
    attachError.value = `一次最多发 ${MAX_IMAGES} 张图片`
    return
  }
  for (const f of files.slice(0, room)) {
    loading.value++
    try {
      const img = await loadImageFile(f)
      // 原图先落盘再入列：反过来的话，用户极快地按发送时可能读不到原图
      await putImage(img.id, img.dataUrl)
      pending.value.push({ id: img.id, thumb: img.thumb, w: img.w, h: img.h, name: img.name })
    } catch (e) {
      attachError.value = (e as Error).message || '图片读取失败'
    } finally {
      loading.value--
    }
  }
  if (files.length > room) attachError.value = `一次最多发 ${MAX_IMAGES} 张图片`
}

function onPick(e: Event) {
  const el = e.target as HTMLInputElement
  void addFiles(Array.from(el.files ?? []))
  el.value = '' // 允许连续选同一张
}

/* ---------------- 文档 ---------------- */

async function addDocs(files: File[]) {
  if (!files.length) return
  attachError.value = ''
  const room = MAX_DOCS - pendingDocs.value.length
  if (room <= 0) {
    attachError.value = `一次最多带 ${MAX_DOCS} 个文件`
    return
  }
  if (files.length > room) attachError.value = `一次最多带 ${MAX_DOCS} 个文件，多出的已忽略`

  // 串行解析：并行会让几个大 PDF 同时吃 CPU，每个都变慢，用户感觉更糟
  for (const f of files.slice(0, room)) {
    docBusy.value++
    try {
      const d = await extractDoc(f)
      pendingDocs.value.push(d)
      // 扫描件的提示不拦截发送（也许另外几页有字够用），只提醒
      if (d.scanned && d.note) showHint(d.note)
    } catch (e) {
      attachError.value = (e as Error).message || '文件解析失败'
    } finally {
      docBusy.value--
    }
  }
}

function onPickDoc(e: Event) {
  const el = e.target as HTMLInputElement
  void addDocs(Array.from(el.files ?? []))
  el.value = ''
}

function removeDoc(id: string) {
  const i = pendingDocs.value.findIndex((x) => x.id === id)
  if (i >= 0) pendingDocs.value.splice(i, 1)
}

function onPaste(e: ClipboardEvent) {
  const files = pickImageFiles(e.clipboardData)
  if (!files.length) return // 没有图片就让它正常粘贴文字
  e.preventDefault()
  void addFiles(files)
}

function onDrop(e: DragEvent) {
  const imgs = pickImageFiles(e.dataTransfer)
  const docs = pickDocFiles(e.dataTransfer)
  if (imgs.length) void addFiles(imgs)
  if (docs.length) void addDocs(docs)
  if (!imgs.length && !docs.length && (e.dataTransfer?.files?.length ?? 0) > 0) {
    attachError.value = '只支持图片、PDF、Word(.docx)、Excel(.xlsx) 和纯文本文件'
  }
}

function removeImage(id: string) {
  const i = pending.value.findIndex((x) => x.id === id)
  if (i >= 0) pending.value.splice(i, 1)
}

function submit() {
  if (props.isStreaming) return
  // 解析中按 Enter 不能静默吞掉——用户会以为键盘坏了
  if (docBusy.value > 0) {
    showHint('文件还在解析，稍等一下')
    return
  }
  const t = text.value.trim()
  const images = pending.value
  const docs = pendingDocs.value
  if (!t && !images.length && !docs.length) return
  emit('send', t, images, docs)
  text.value = ''
  pending.value = []
  pendingDocs.value = []
}

function onKeydown(e: KeyboardEvent) {
  // Enter 发送，Shift+Enter 换行
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submit()
  }
}

const hint = ref('')
let hintTimer: number | undefined
let starting: Promise<void> | null = null

function showHint(msg: string) {
  hint.value = msg
  if (hintTimer) clearTimeout(hintTimer)
  hintTimer = window.setTimeout(() => {
    hint.value = ''
  }, 2400)
}

/** 按住说话：按下开始采集，松开结束并识别。 */
async function onMicDown(e: PointerEvent) {
  if (props.isStreaming || rec.isBusy.value || !rec.isSupported.value) return
  if (e.pointerType === 'mouse' && e.button !== 0) return
  if (rec.isRecording.value) return

  hint.value = ''
  rec.clearError()
  try {
    // 捕获指针：按住后手指/鼠标滑出按钮，pointerup 仍然落在这里
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  } catch {
    /* 老浏览器不支持指针捕获，退化成普通按钮行为 */
  }
  starting = rec.start()
  try {
    await starting
  } finally {
    starting = null
  }
}

async function onMicUp() {
  // start() 要等 getUserMedia，按得快时 pointerup 可能早于它完成
  if (starting) {
    try {
      await starting
    } catch {
      /* start 内部已把错误写进 rec.error */
    }
    starting = null
  }
  if (!rec.isRecording.value) return

  const recognized = await rec.stopAndTranscribe()
  if (recognized) {
    // 回填到输入框，用户可以改错字再发（ASR 一定有错字，不能直接发出去）
    text.value = text.value ? `${text.value}${recognized}` : recognized
    await nextTick()
    textarea.value?.focus()
  } else if (!rec.error.value) {
    // 内部判定为误触（按太短）或整段静音
    showHint('按住说话，说完再松开')
  }
}

function onMicCancel() {
  if (rec.isRecording.value) rec.cancel()
}

const mmss = (s: number) =>
  `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`

// 录音过程中禁用输入框，避免边录边打字造成的焦点混乱
watch(
  () => rec.isRecording.value,
  (v) => {
    if (v) textarea.value?.blur()
  },
)
</script>

<template>
  <div class="input-wrap" @dragover.prevent @drop.prevent="onDrop" @paste="onPaste">
    <div v-if="rec.error.value" class="rec-error">
      <span>{{ rec.error.value }}</span>
      <button class="link" @click="rec.clearError">知道了</button>
    </div>
    <div v-else-if="attachError" class="rec-error">
      <span>{{ attachError }}</span>
      <button class="link" @click="attachError = ''">知道了</button>
    </div>
    <div v-else-if="hint" class="rec-hint">{{ hint }}</div>

    <div v-if="pending.length || loading || pendingDocs.length || docBusy" class="attach-strip">
      <div v-for="img in pending" :key="img.id" class="attach-item">
        <img :src="img.thumb" :alt="img.name" class="attach-thumb" />
        <button class="attach-del" title="移除" @click="removeImage(img.id)">×</button>
      </div>
      <div v-for="d in pendingDocs" :key="d.id" class="doc-chip" :title="d.note || d.name">
        <svg class="doc-ico" viewBox="0 0 24 24" aria-hidden="true">
          <path
            d="M6 3.5h7l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-16a1 1 0 0 1 1-1z"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linejoin="round"
          />
          <path d="M13 3.5v5h5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
        </svg>
        <span class="doc-body">
          <span class="doc-name">{{ d.name }}</span>
          <span class="doc-meta">{{ docMeta(d) }}</span>
        </span>
        <button class="doc-del" title="移除" @click="removeDoc(d.id)">×</button>
      </div>
      <div v-if="loading || docBusy" class="attach-item attach-loading">
        <span class="spinner"></span>
      </div>
      <span v-if="loading || docBusy" class="attach-count">解析中…</span>
      <span v-else-if="pending.length" class="attach-count">{{ pending.length }}/{{ MAX_IMAGES }}</span>
    </div>

    <div class="input-bar">
      <button class="btn ghost" :disabled="isStreaming" @click="emit('clear')">
        清空<span class="hide-sm">对话</span>
      </button>

      <button
        class="btn pic"
        :disabled="isStreaming"
        title="发送图片：也可以直接粘贴或拖进来"
        @click="fileInput?.click()"
      >
        <svg class="pic-icon" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3.5" y="5" width="17" height="14" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.6" />
          <circle cx="9" cy="10.5" r="1.6" fill="currentColor" />
          <path d="M5 17l4.5-4.5L13 16l3-3 3.5 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>
      <input
        ref="fileInput"
        class="file-input"
        type="file"
        :accept="ACCEPT"
        multiple
        @change="onPick"
      />

      <button
        class="btn doc"
        :disabled="isStreaming"
        title="发送文件：PDF / Word(.docx) / Excel(.xlsx) / 纯文本，也可以直接拖进来"
        @click="docInput?.click()"
      >
        <svg class="doc-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path
            d="M7 3.5h6.5l4.5 4.5v12.5a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1z"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linejoin="round"
          />
          <path d="M13.5 3.5V8H18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
          <path d="M9 12.5h6M9 16h4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
        </svg>
      </button>
      <input
        ref="docInput"
        class="file-input"
        type="file"
        :accept="DOC_ACCEPT"
        multiple
        @change="onPickDoc"
      />

      <button
        class="btn mic"
        :class="{
          recording: rec.isRecording.value,
          busy: rec.isBusy.value,
        }"
        :disabled="isStreaming || !rec.isSupported.value || rec.isBusy.value"
        :title="
          !rec.isSupported.value
            ? '当前浏览器不支持录音'
            : rec.isRecording.value
              ? '松开结束并识别'
              : `按住说话（最长 ${MAX_SECONDS} 秒）`
        "
        @pointerdown.prevent="onMicDown"
        @pointerup.prevent="onMicUp"
        @pointercancel="onMicCancel"
        @contextmenu.prevent
      >
        <template v-if="rec.isBusy.value">
          <span class="spinner"></span>
          <span class="mic-label">识别中…</span>
        </template>
        <template v-else-if="rec.isRecording.value">
          <span class="stop-square"></span>
          <span class="mic-label mono">{{ mmss(rec.seconds.value) }}</span>
        </template>
        <template v-else>
          <svg class="mic-icon" viewBox="0 0 24 24" aria-hidden="true">
            <path
              d="M12 15a3.5 3.5 0 0 0 3.5-3.5v-5a3.5 3.5 0 1 0-7 0v5A3.5 3.5 0 0 0 12 15Z"
              fill="currentColor"
            />
            <path
              d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3.5M8.5 21.5h7"
              fill="none"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linecap="round"
            />
          </svg>
        </template>
      </button>

      <textarea
        ref="textarea"
        v-model="text"
        class="input"
        :placeholder="
          rec.isRecording.value
            ? '正在收音…松开话筒结束'
            : '输入消息，Enter 发送；可拖入图片或文件（PDF/Word/Excel），或按住话筒说话'
        "
        :disabled="isStreaming || rec.isRecording.value"
        rows="2"
        @keydown="onKeydown"
      ></textarea>

      <button
        v-if="!isStreaming"
        class="btn primary"
        :disabled="(!text.trim() && !pending.length && !pendingDocs.length) || rec.isRecording.value || loading > 0 || docBusy > 0"
        @click="submit"
      >
        {{ docBusy > 0 ? '解析中' : '发送' }}
      </button>
      <button v-else class="btn danger" @click="emit('stop')">停止生成</button>
    </div>
  </div>
</template>

<style scoped>
.input-wrap {
  flex: none;
  border-top: 1px solid var(--border);
  background: var(--card);
}

.rec-error {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px 0;
  font-size: 13px;
  color: #b91c1c;
}

.rec-hint {
  padding: 8px 16px 0;
  font-size: 12px;
  color: var(--text-secondary);
}

/* ---- 待发送图片 ---- */
.attach-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px 0;
  flex-wrap: wrap;
}

.attach-item {
  position: relative;
  width: 56px;
  height: 56px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border);
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
}

.attach-thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.attach-del {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 16px;
  height: 16px;
  line-height: 14px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: rgb(0 0 0 / 55%);
  color: #fff;
  font-size: 12px;
  cursor: pointer;
}

.attach-del:hover {
  background: #dc2626;
}

.attach-loading .spinner {
  color: var(--text-secondary);
}

.attach-count {
  font-size: 11px;
  color: var(--text-secondary);
}

.file-input {
  display: none;
}

.rec-error .link {
  border: none;
  background: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  text-decoration: underline;
}

.input-bar {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 12px 16px;
}

.input {
  flex: 1 1 auto;
  resize: none;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 12px;
  font: inherit;
  color: var(--text);
  background: var(--bg);
  outline: none;
}

.input:focus {
  border-color: var(--accent);
}

.input:disabled {
  opacity: 0.6;
}

.btn {
  flex: none;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 16px;
  font: inherit;
  cursor: pointer;
  background: var(--card);
  color: var(--text);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

.btn.danger {
  background: #dc2626;
  border-color: #dc2626;
  color: #fff;
}

.btn.ghost {
  color: var(--text-secondary);
}

/* ---- 图片按钮 ---- */
.btn.pic {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 46px;
  padding: 10px 12px;
  color: var(--text-secondary);
}

.btn.pic:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent);
}

.pic-icon {
  width: 18px;
  height: 18px;
}

/* ---- 文件按钮 ---- */
.btn.doc {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 46px;
  padding: 10px 12px;
  color: var(--text-secondary);
}

.btn.doc:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent);
}

.doc-icon {
  width: 18px;
  height: 18px;
}

/* ---- 已选文档 ---- */
.doc-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 260px;
  padding: 6px 8px 6px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  color: var(--text);
}

.doc-ico {
  flex: none;
  width: 18px;
  height: 18px;
  color: var(--accent);
}

.doc-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: 1.25;
}

.doc-name {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc-meta {
  font-size: 11px;
  color: var(--text-secondary);
}

.doc-del {
  flex: none;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-secondary);
  font-size: 14px;
  line-height: 16px;
  cursor: pointer;
}

.doc-del:hover {
  background: #dc2626;
  color: #fff;
}

/* ---- 话筒按钮 ---- */
.btn.mic {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-width: 46px;
  padding: 10px 12px;
  color: var(--text-secondary);
  transition: color 0.15s, border-color 0.15s, background 0.15s;
  /* 按住说话：禁掉手势滚动与长按选中，否则触屏一按就变成拖页面 */
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
  -webkit-touch-callout: none;
}

.btn.mic:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent);
}

.btn.mic.recording {
  color: #fff;
  background: #dc2626;
  border-color: #dc2626;
}

.btn.mic.busy {
  color: var(--accent);
  border-color: var(--accent);
}

.mic-icon {
  width: 18px;
  height: 18px;
}

.mic-label {
  font-size: 13px;
}

.mono {
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
}

.stop-square {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  background: #fff;
}

.spinner {
  width: 12px;
  height: 12px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 640px) {
  .input-bar {
    gap: 6px;
    padding: 10px 12px;
  }

  .btn {
    padding: 9px 11px;
  }

  .hide-sm {
    display: none;
  }
}
</style>
