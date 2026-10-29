<script setup lang="ts">
import { computed } from 'vue'
import type { BalanceInfo } from '../api/balance'

const props = defineProps<{
  balance: BalanceInfo | null
  loading: boolean
  error: string
}>()

const emit = defineEmits<{ (e: 'refresh'): void }>()

const symbol = computed(() => (props.balance?.currency === 'USD' ? '$' : '¥'))

const breakdown = computed(() => {
  const b = props.balance
  if (!b) return ''
  return `赠金 ${symbol.value}${b.granted_balance} + 充值 ${symbol.value}${b.topped_up_balance}`
})
</script>

<template>
  <div class="balance">
    <span v-if="error" class="err" :title="error">余额获取失败</span>

    <template v-else-if="balance?.ok">
      <span class="label">余额</span>
      <span class="value" :title="breakdown">
        {{ symbol }}{{ balance.total_balance }}
      </span>
      <span v-if="balance.is_available === false" class="low">不足</span>
    </template>

    <span v-else class="label">{{ loading ? '余额加载中…' : '余额' }}</span>

    <button
      class="refresh"
      :disabled="loading"
      title="刷新余额"
      @click="emit('refresh')"
    >
      {{ loading ? '…' : '↻' }}
    </button>
  </div>
</template>

<style scoped>
.balance {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-secondary);
}

.label {
  color: var(--text-secondary);
}

.value {
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
  cursor: default;
}

.low {
  color: #dc2626;
  font-size: 12px;
}

.err {
  color: #dc2626;
  cursor: default;
}

.refresh {
  border: 1px solid var(--border);
  background: var(--card);
  color: var(--text-secondary);
  border-radius: 6px;
  width: 22px;
  height: 22px;
  line-height: 1;
  cursor: pointer;
  font-size: 12px;
  padding: 0;
}

.refresh:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent);
}

.refresh:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
