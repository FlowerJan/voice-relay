<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { useChat } from '../composables/useChat'
import { useBalance } from '../composables/useBalance'
import MessageList from '../components/MessageList.vue'
import ChatInput from '../components/ChatInput.vue'
import BalanceBadge from '../components/BalanceBadge.vue'

const { messages, isStreaming, thinking, send, stop, clear } = useChat()
const { balance, loading: balanceLoading, error: balanceError, refresh: refreshBalance } =
  useBalance()

// 每轮对话结束后刷新一次余额（对话越多，扣得越多，要让用户随时看得到）
watch(isStreaming, (now, before) => {
  if (before === true && now === false) refreshBalance()
})

onMounted(refreshBalance)
</script>

<template>
  <div class="chat-view">
    <header class="topbar">
      <span class="title">小智 · 语音文字智能体</span>
      <span class="subtitle">第一阶段 · 文字聊天</span>
      <BalanceBadge
        class="balance-slot"
        :balance="balance"
        :loading="balanceLoading"
        :error="balanceError"
        @refresh="refreshBalance"
      />
    </header>
    <MessageList :messages="messages" :thinking="thinking" />
    <ChatInput :is-streaming="isStreaming" @send="send" @stop="stop" @clear="clear" />
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  max-width: 860px;
  margin: 0 auto;
  background: var(--bg);
}

.topbar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--card);
}

.title {
  font-weight: 600;
  font-size: 16px;
}

.subtitle {
  font-size: 12px;
  color: var(--text-secondary);
}

.balance-slot {
  margin-left: auto;
}
</style>
