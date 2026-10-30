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
        error.value = data.message ?? '余额获取失败'
      } else {
        balance.value = data
      }
    } catch {
      error.value = '余额获取失败'
    } finally {
      loading.value = false
    }
  }

  return { balance, loading, error, refresh }
}
