<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { searchSessions, useSessions } from '../composables/useSessions'

const props = defineProps<{
  streaming: boolean
  open: boolean
}>()

const emit = defineEmits<{
  (e: 'new'): void
  (e: 'switch', id: string): void
  (e: 'delete', id: string): void
  (e: 'rename', id: string, title: string): void
  (e: 'export'): void
  (e: 'close'): void
}>()

const { ordered, currentId, storageError } = useSessions()

const editingId = ref('')
const draft = ref('')

function startRename(id: string, title: string) {
  editingId.value = id
  draft.value = title
  // 注意：不能在这里用 ref="inputRef" 去拿输入框。
  // 这个 input 在 v-for 里面，Vue 3 会把 v-for 中的字符串 ref 收集成数组，
  // inputRef.value 拿到的是数组而不是元素，.focus() 会直接抛 TypeError。
  // 同一时刻只存在一个编辑框，用 DOM 查询反而更稳。
  nextTick(() => {
    const el = document.querySelector<HTMLInputElement>('.rename-input')
    el?.focus()
    el?.select()
  })
}

function commitRename() {
  const id = editingId.value
  if (!id) return
  const t = draft.value.trim()
  if (t) emit('rename', id, t)
  editingId.value = ''
}

function cancelRename() {
  editingId.value = ''
}

function fmt(ts: number): string {
  const d = new Date(ts)
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  const pad = (n: number) => String(n).padStart(2, '0')
  return sameDay ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${d.getMonth() + 1}/${d.getDate()}`
}

/* ---------------- 搜索 ---------------- */

const q = ref('')

/**
 * 不做防抖：本地会话是几十个、每屏几千字量级，一次全量扫描是微秒级；
 * 加防抖反而会让输入框有延迟感。
 */
const hits = computed(() => searchSessions(ordered.value, q.value))

const count = computed(() => hits.value.length)

const searchRef = ref<HTMLInputElement | null>(null)

function clearSearch() {
  q.value = ''
  searchRef.value?.focus()
}

/** 切换会话后清空搜索，否则列表还停在上一个关键词的筛选结果里，很容易迷路。 */
watch(currentId, () => {
  if (q.value) q.value = ''
})
</script>

<template>
  <div class="side-backdrop" :class="{ show: props.open }" @click="emit('close')"></div>

  <aside class="sidebar" :class="{ open: props.open }">
    <div class="side-head">
      <button class="new-btn" :disabled="props.streaming" @click="emit('new')">
        <svg viewBox="0 0 24 24" class="ico" aria-hidden="true">
          <path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
        </svg>
        新对话
      </button>
      <button class="icon-btn close-btn" title="收起" @click="emit('close')">
        <svg viewBox="0 0 24 24" class="ico" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
        </svg>
      </button>
    </div>

    <div class="side-search">
      <svg viewBox="0 0 24 24" class="search-ico" aria-hidden="true">
        <circle cx="11" cy="11" r="6.5" fill="none" stroke="currentColor" stroke-width="1.6" />
        <path d="M16 16l4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
      </svg>
      <input
        ref="searchRef"
        v-model="q"
        class="search-input"
        type="search"
        placeholder="搜索对话内容"
        @keydown.esc.prevent="clearSearch"
      />
      <button v-if="q" class="icon-btn search-clear" title="清除" @click="clearSearch">
        <svg viewBox="0 0 24 24" class="ico" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
        </svg>
      </button>
    </div>

    <div class="side-list">
      <p v-if="count === 0" class="side-empty">
        {{ q ? '没有匹配的对话' : '还没有对话' }}
      </p>

      <div
        v-for="h in hits"
        :key="h.session.id"
        class="side-item"
        :class="{ active: h.session.id === currentId }"
      >
        <input
          v-if="editingId === h.session.id"
          v-model="draft"
          class="rename-input"
          maxlength="40"
          @keydown.enter.prevent="commitRename"
          @keydown.esc.prevent="cancelRename"
          @blur="commitRename"
        />
        <button
          v-else
          class="item-main"
          :title="h.session.title"
          @click="emit('switch', h.session.id)"
          @dblclick="startRename(h.session.id, h.session.title)"
        >
          <span class="item-title">{{ h.session.title }}</span>
          <span v-if="h.snippet" class="item-snippet">{{ h.snippet }}</span>
          <span class="item-meta">
            <span class="item-time">{{ fmt(h.session.updatedAt) }}</span>
            <span v-if="h.session.messages.length" class="item-msgs">
              {{ h.session.messages.length }} 条
            </span>
          </span>
        </button>

        <div v-if="editingId !== h.session.id" class="item-ops">
          <button class="icon-btn" title="重命名" @click.stop="startRename(h.session.id, h.session.title)">
            <svg viewBox="0 0 24 24" class="ico" aria-hidden="true">
              <path
                d="M4 20h4l10-10a2.1 2.1 0 0 0-3-3L5 17v3zM14.5 6.5l3 3"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </button>
          <button class="icon-btn danger" title="删除" @click.stop="emit('delete', h.session.id)">
            <svg viewBox="0 0 24 24" class="ico" aria-hidden="true">
              <path
                d="M5 7h14M9 7V5h6v2M7 7l1 13h8l1-13M10 11v6M14 11v6"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </button>
        </div>
      </div>
    </div>

    <div class="side-foot">
      <button class="foot-btn" @click="emit('export')">
        <svg viewBox="0 0 24 24" class="ico" aria-hidden="true">
          <path
            d="M12 4v11m0 0l-4-4m4 4l4-4M5 19h14"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        导出为 Markdown
      </button>
      <p v-if="storageError" class="storage-warn">{{ storageError }}</p>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  flex: none;
  width: 240px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  background: var(--card);
  min-height: 0;
}

.side-backdrop {
  display: none;
}

.side-head {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 12px 10px;
  border-bottom: 1px solid var(--border);
}

.new-btn {
  flex: 1 1 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg);
  color: var(--text);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.new-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}

.new-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.close-btn {
  display: none;
}

.side-search {
  flex: none;
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px 4px;
}

.search-ico {
  position: absolute;
  left: 19px;
  width: 14px;
  height: 14px;
  color: var(--text-secondary);
  pointer-events: none;
}

.search-input {
  flex: 1 1 auto;
  min-width: 0;
  font: inherit;
  font-size: 12px;
  padding: 6px 26px 6px 28px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  color: var(--text);
  outline: none;
}

.search-input::placeholder {
  color: var(--text-secondary);
}

.search-input:focus {
  border-color: var(--accent);
}

/* Chrome 自带的清除按钮会和我们自己的 × 重叠 */
.search-input::-webkit-search-cancel-button {
  display: none;
}

.search-clear {
  position: absolute;
  right: 14px;
  padding: 3px;
}

.item-snippet {
  font-size: 11px;
  line-height: 1.4;
  color: var(--text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-all;
}

.side-list {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.side-empty {
  margin: 20px 8px;
  font-size: 13px;
  color: var(--text-secondary);
  text-align: center;
}

.side-item {
  display: flex;
  align-items: center;
  gap: 2px;
  border-radius: 9px;
  padding: 2px;
}

.side-item:hover {
  background: var(--bg);
}

.side-item.active {
  background: var(--bg);
  box-shadow: inset 0 0 0 1px var(--accent);
}

.item-main {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  border: none;
  background: none;
  padding: 7px 8px;
  text-align: left;
  font: inherit;
  cursor: pointer;
  color: var(--text);
}

.item-title {
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.item-meta {
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: var(--text-secondary);
}

.item-ops {
  flex: none;
  display: flex;
  gap: 1px;
  opacity: 0;
  transition: opacity 0.12s;
}

.side-item:hover .item-ops,
.side-item.active .item-ops {
  opacity: 1;
}

.icon-btn {
  border: none;
  background: none;
  color: var(--text-secondary);
  padding: 5px;
  border-radius: 6px;
  cursor: pointer;
  display: inline-flex;
}

.icon-btn:hover {
  background: var(--border);
  color: var(--text);
}

.icon-btn.danger:hover {
  color: #dc2626;
}

.ico {
  width: 14px;
  height: 14px;
}

.rename-input {
  flex: 1 1 auto;
  min-width: 0;
  font: inherit;
  font-size: 13px;
  padding: 7px 8px;
  border: 1px solid var(--accent);
  border-radius: 7px;
  background: var(--card);
  color: var(--text);
  outline: none;
}

.side-foot {
  flex: none;
  padding: 10px;
  border-top: 1px solid var(--border);
}

.foot-btn {
  width: 100%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  color: var(--text-secondary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.foot-btn:hover {
  color: var(--accent);
  border-color: var(--accent);
}

.storage-warn {
  margin: 8px 2px 0;
  font-size: 11px;
  line-height: 1.5;
  color: #92400e;
}

@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    z-index: 30;
    width: 78vw;
    max-width: 300px;
    transform: translateX(-100%);
    transition: transform 0.2s ease;
    box-shadow: 0 0 24px rgb(0 0 0 / 18%);
  }

  .sidebar.open {
    transform: translateX(0);
  }

  .side-backdrop.show {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 20;
    background: rgb(0 0 0 / 35%);
  }

  .close-btn {
    display: inline-flex;
  }
}
</style>
