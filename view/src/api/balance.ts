// 账户余额查询：后端 GET /api/balance 代理了 DeepSeek 官方的 /user/balance，
// 前端不直接持有 API Key。
//
// 这个接口有**两条形状不同的失败路径**，必须分开处理：
// - 8000 自己出错 → `{ok: false, message}` + 4xx/5xx
// - relay 出错（8000 没起、超时）→ `{error: {code, message, hint}}` + 5xx
// 以前不看状态码就把 body 当 `BalanceInfo` 用，第二条会得到 `ok: undefined`，
// 于是既不满足 `ok === false` 也不满足 `ok === true`，界面安静地显示一个空的「余额」。
import { readApiError } from './errors'

export interface BalanceInfo {
  ok: boolean
  message?: string
  /** 现在能做什么。失败时界面挂在 title 上，不占那一行小字的宽度。 */
  hint?: string
  is_available?: boolean
  currency?: string
  total_balance?: string
  granted_balance?: string
  topped_up_balance?: string
}

/** 余额查不到不影响对话，这句在任何失败下都成立。 */
const FALLBACK_HINT = '余额只是展示，不影响继续对话；点 ↻ 再试一次'

export async function fetchBalance(): Promise<BalanceInfo> {
  let resp: Response
  try {
    resp = await fetch('/api/balance')
  } catch {
    return { ok: false, message: '网络连接失败', hint: '确认网关（9000）在运行；刷新页面后再试' }
  }

  if (!resp.ok) {
    const info = await readApiError(resp)
    return { ok: false, message: info.message, hint: info.hint ?? FALLBACK_HINT }
  }

  try {
    const data = (await resp.json()) as BalanceInfo
    // 只认 ok === true。字段缺失说明形状不对，不能当成功——
    // 那正是"空余额"的来源：渲染时 total_balance 是 undefined，界面看起来像加载完了。
    if (data?.ok === true) return data
    return {
      ok: false,
      message: data?.message || '余额接口返回了非预期内容',
      hint: FALLBACK_HINT,
    }
  } catch {
    return {
      ok: false,
      message: `余额接口返回了非 JSON（HTTP ${resp.status}）`,
      hint: FALLBACK_HINT,
    }
  }
}
