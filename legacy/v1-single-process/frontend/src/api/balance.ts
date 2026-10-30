// 账户余额查询：后端 GET /api/balance 代理了 DeepSeek 官方的 /user/balance，
// 前端不直接持有 API Key。

export interface BalanceInfo {
  ok: boolean
  message?: string
  is_available?: boolean
  currency?: string
  total_balance?: string
  granted_balance?: string
  topped_up_balance?: string
}

export async function fetchBalance(): Promise<BalanceInfo> {
  let resp: Response
  try {
    resp = await fetch('/api/balance')
  } catch {
    return { ok: false, message: '网络连接失败' }
  }
  try {
    return (await resp.json()) as BalanceInfo
  } catch {
    return { ok: false, message: `请求失败：${resp.status}` }
  }
}
