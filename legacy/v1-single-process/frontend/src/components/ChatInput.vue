<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{
  isStreaming: boolean
}>()

const emit = defineEmits<{
  (e: 'send', text: string): void
  (e: 'stop'): void
  (e: 'clear'): void
}>()

const text = ref('')

function submit() {
  const t = text.value.trim()
  if (!t || props.isStreaming) return
  emit('send', t)
  text.value = ''
}

function onKeydown(e: KeyboardEvent) {
  // Enter 发送，Shift+Enter 换行
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submit()
  }
}
</script>

<template>
  <div class="input-bar">
    <button class="btn ghost" :disabled="isStreaming" @click="emit('clear')">
      清空对话
    </button>

    <textarea
      v-model="text"
      class="input"
      placeholder="输入消息，Enter 发送，Shift+Enter 换行"
      :disabled="isStreaming"
      rows="2"
      @keydown="onKeydown"
    ></textarea>

    <button
      v-if="!isStreaming"
      class="btn primary"
      :disabled="!text.trim()"
      @click="submit"
    >
      发送
    </button>
    <button v-else class="btn danger" @click="emit('stop')">停止生成</button>
  </div>
</template>

<style scoped>
.input-bar {
  flex: none;
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  background: var(--card);
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
</style>
