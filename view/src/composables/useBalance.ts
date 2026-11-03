import { ref } from 'vue'
import { fetchBalance, type BalanceInfo } from '../api/balance'

export function useBalance() {
  const balance = ref<BalanceInfo | null>(null)
  const loading = ref(false)
  const error = ref('')

  async function refresh() {
    loading.value = true
    error.value = ''
    try {
      const data = await fetchBalance()
      if (data.ok === false) {
        // 建议挂进同一句：这里只有一个行内位置，分成两行会被挤成省略号。
        error.value = data.hint
          ? `${data.message ?? '余额获取失败'}。${data.hint}`
          : data.message ?? '余额获取失败'
      } else {
        balance.value = data
      }
    } catch {
      error.value = '余额获取失败。余额只是展示，不影响继续对话；点 ↻ 再试一次'
    } finally {
      loading.value = false
    }
  }

  return { balance, loading, error, refresh }
}
