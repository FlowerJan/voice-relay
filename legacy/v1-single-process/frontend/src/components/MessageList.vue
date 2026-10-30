<script setup lang="ts">
import { ref, watch, nextTick, computed } from 'vue'
import { renderMarkdown } from '../utils/markdown'
import type { Message } from '../composables/useChat'

const props = defineProps<{
  messages: Message[]
  thinking: boolean
}>()

const listRef = ref<HTMLElement | null>(null)
const lastIdx = computed(() => props.messages.length - 1)

function scrollToBottom() {
  nextTick(() => {
    const el = listRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

// 消息数或任意内容变化都触发滚动（自动滚动到底）
watch(
  () => props.messages.map((m) => m.content + '|' + (m.reasoning ?? '')).join('@'),
  scrollToBottom,
)
</script>

<template>
  <div class="message-list" ref="listRef">
    <div v-if="messages.length === 0" class="empty">
      还没有对话，下面输入一句话开始吧。
    </div>

    <div
      v-for="(m, i) in messages"
      :key="i"
      class="row"
      :class="m.role"
    >
      <div class="avatar">{{ m.role === 'user' ? '我' : '智' }}</div>

      <div class="bubble" :class="{ user: m.role === 'user', error: m.error }">
        <template v-if="m.role === 'assistant' && m.reasoning">
          <details class="reasoning">
            <summary>思考过程</summary>
            <div class="reasoning-body" v-html="renderMarkdown(m.reasoning)"></div>
          </details>
        </template>

        <div v-if="m.error" class="content error-text">{{ m.content }}</div>
        <div
          v-else-if="m.content"
          class="content"
          v-html="renderMarkdown(m.content)"
        ></div>
        <div
          v-else-if="thinking && i === lastIdx"
          class="thinking"
        >
          <span class="dot"></span><span class="dot"></span><span class="dot"></span>
          思考中
        </div>

        <div v-if="m.truncated" class="truncated">
          ⚠️ 回答达到长度上限被截断，以上内容不完整
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.message-list {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
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

.error-text {
  color: #dc2626;
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
.content :deep(pre.hljs) {
  background: var(--code-bg);
  border-radius: 8px;
  padding: 12px;
  overflow-x: auto;
  margin: 8px 0;
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
</style>
