<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useChat, type MsgImage } from '../composables/useChat'
import type { MsgDoc } from '../utils/doc'
import { useBalance } from '../composables/useBalance'
import { useSessions } from '../composables/useSessions'
import { useTTS } from '../composables/useTTS'
import MessageList from '../components/MessageList.vue'
import ChatInput from '../components/ChatInput.vue'
import BalanceBadge from '../components/BalanceBadge.vue'
import SessionSidebar from '../components/SessionSidebar.vue'

const {
  current,
  currentMessages,
  currentId,
  create,
  select,
  rename,
  remove,
  clearCurrent,
  touch,
  exportCurrent,
} = useSessions()

const { isStreaming, thinking, send, stop, regenerate, retry, resendFrom } = useChat(
  () => currentMessages.value,
)
const { balance, loading: balanceLoading, error: balanceError, refresh: refreshBalance } =
  useBalance()
const tts = useTTS()

const sidebarOpen = ref(false)

const title = computed(() => current.value?.title ?? '小智')
const isEmpty = computed(() => !currentMessages.value.length)

const ttsLabel = computed(() => {
  if (tts.speaking.value) {
    return tts.total.value > 1 ? `播报 ${tts.index.value + 1}/${tts.total.value}` : '播报中'
  }
  return tts.enabled.value ? '语音开' : '语音关'
})

/** 每轮对话结束做三件事：落标题、刷余额、整段播报。 */
watch(isStreaming, (now, before) => {
  if (!(before === true && now === false)) return
  touch()
  refreshBalance()

  const list = currentMessages.value
  const last = list[list.length - 1]
  if (last?.role === 'assistant' && !last.error && last.content.trim()) {
    void tts.speak(last.content)
  }
})

/** 清空/切换会话时停掉播报，否则声音还在念已经不在屏幕上的内容。 */
watch(currentId, () => {
  tts.stop()
})

function onSend(text: string, images: MsgImage[] = [], docs: MsgDoc[] = []) {
  tts.stop() // 上一轮的播报还没念完就先掐掉，别和这一轮抢
  const task = send(text, images, docs)
  touch() // 立刻用首条消息起标题，不用等回答结束
  void task
}

function onStop() {
  stop()
  tts.stop()
}

function onClear() {
  if (isEmpty.value) return
  if (!window.confirm('清空当前对话？清空后无法恢复，需要的话先导出。')) return
  if (isStreaming.value) stop()
  tts.stop()
  clearCurrent()
}

function onNew() {
  if (isStreaming.value) stop()
  tts.stop()
  create()
  sidebarOpen.value = false
}

function onSwitch(id: string) {
  sidebarOpen.value = false
  if (id === currentId.value) return
  if (isStreaming.value) stop()
  tts.stop()
  select(id)
}

function onDelete(id: string) {
  const target = current.value
  const hasContent = target?.id === id && target.messages.length > 0
  if (hasContent && !window.confirm('删除这个对话？删除后无法恢复。')) return
  if (isStreaming.value && currentId.value === id) stop()
  remove(id)
}

function onExport() {
  if (isEmpty.value) return
  exportCurrent()
}

function onRegenerate() {
  tts.stop()
  void regenerate()
}

function onRetry(index: number) {
  tts.stop()
  void retry(index)
}

function onResend(index: number, text: string) {
  tts.stop()
  void resendFrom(index, text)
}

/** Esc 停止生成：不用把光标移到按钮上。 */
function onGlobalKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && isStreaming.value) {
    e.preventDefault()
    onStop()
  }
}

onMounted(() => {
  refreshBalance()
  window.addEventListener('keydown', onGlobalKey)
})

onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKey))
</script>

<template>
  <div class="app">
    <SessionSidebar
      :streaming="isStreaming"
      :open="sidebarOpen"
      @new="onNew"
      @switch="onSwitch"
      @delete="onDelete"
      @rename="rename"
      @export="onExport"
      @close="sidebarOpen = false"
    />

    <div class="chat-view">
      <header class="topbar">
        <button class="menu-btn" title="对话列表" @click="sidebarOpen = true">
          <svg viewBox="0 0 24 24" class="menu-ico" aria-hidden="true">
            <path
              d="M4 7h16M4 12h16M4 17h10"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
            />
          </svg>
        </button>

        <span class="title" :title="title">{{ title }}</span>

        <div class="topbar-right">
          <button
            class="tts-btn"
            :class="{ on: tts.enabled.value && !tts.speaking.value, speaking: tts.speaking.value }"
            :title="tts.enabled.value ? '关闭语音播报' : '开启语音播报'"
            @click="tts.toggle"
          >
            <svg viewBox="0 0 24 24" class="tts-icon" aria-hidden="true">
              <path d="M4 9.5h3l4-3.5v12l-4-3.5H4z" fill="currentColor" />
              <path
                v-if="tts.enabled.value"
                d="M15 8.5a4.5 4.5 0 0 1 0 7M17.5 6a8 8 0 0 1 0 12"
                fill="none"
                stroke="currentColor"
                stroke-width="1.6"
                stroke-linecap="round"
              />
              <path
                v-else
                d="M15.5 9l5 5m0-5l-5 5"
                fill="none"
                stroke="currentColor"
                stroke-width="1.6"
                stroke-linecap="round"
              />
            </svg>
            <span class="tts-text">{{ ttsLabel }}</span>
          </button>

          <button v-if="tts.speaking.value" class="tts-stop" @click="tts.stop">停止播报</button>

          <BalanceBadge
            :balance="balance"
            :loading="balanceLoading"
            :error="balanceError"
            @refresh="refreshBalance"
          />
        </div>
      </header>

      <div v-if="tts.error.value" class="tts-error">{{ tts.error.value }}</div>

      <MessageList
        :key="currentId"
        :messages="currentMessages"
        :thinking="thinking"
        :is-streaming="isStreaming"
        @regenerate="onRegenerate"
        @retry="onRetry"
        @resend="onResend"
      />
      <ChatInput
        :is-streaming="isStreaming"
        @send="onSend"
        @stop="onStop"
        @clear="onClear"
      />
    </div>
  </div>
</template>

<style scoped>
.app {
  display: flex;
  height: 100%;
  min-height: 0;
}

.chat-view {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  max-width: 860px;
  margin: 0 auto;
  background: var(--bg);
}

.topbar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--card);
}

.menu-btn {
  display: none;
  border: 1px solid var(--border);
  background: var(--card);
  color: var(--text-secondary);
  border-radius: 8px;
  padding: 5px;
  cursor: pointer;
}

.menu-ico {
  width: 18px;
  height: 18px;
  display: block;
}

.title {
  font-weight: 500;
  font-size: 15px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.topbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
}

.tts-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 5px 11px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  background: var(--card);
  color: var(--text-secondary);
}

.tts-btn.on {
  color: var(--accent);
  border-color: var(--accent);
}

.tts-btn.speaking {
  color: #fff;
  background: var(--accent);
  border-color: var(--accent);
}

.tts-icon {
  width: 15px;
  height: 15px;
}

.tts-stop {
  border: 1px solid #dc2626;
  color: #dc2626;
  background: var(--card);
  border-radius: 999px;
  padding: 5px 11px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.tts-error {
  flex: none;
  padding: 8px 20px;
  font-size: 13px;
  color: #b91c1c;
  background: #fef2f2;
  border-bottom: 1px solid #fecaca;
}

@media (max-width: 768px) {
  .menu-btn {
    display: inline-flex;
  }

  .tts-text {
    display: none;
  }

  .tts-btn {
    padding: 5px 9px;
  }
}
</style>
