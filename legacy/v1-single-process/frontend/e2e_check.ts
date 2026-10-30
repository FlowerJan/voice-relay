// 端到端验证：跑前端真实源码（useChat.ts + api/chat.ts），经 5173 代理打到后端，
// 确认回答是"逐字流式出现"，而不是像修复前那样冻结在第一帧。
import { useChat } from './src/composables/useChat'
import { useBalance } from './src/composables/useBalance'

const BASE = 'http://localhost:5173'
const origFetch = globalThis.fetch
globalThis.fetch = ((input: any, init?: any) => {
  if (typeof input === 'string' && input.startsWith('/')) {
    return origFetch(BASE + input, init)
  }
  return origFetch(input, init)
}) as typeof fetch

const { messages, isStreaming, send } = useChat()

// 采样正文长度变化，统计"增长次数"——这正是修复前会归零的指标
let growth = 0
let lastLen = 0
const samples: number[] = []
const timer = setInterval(() => {
  const m = messages.value[1]
  if (m) {
    if (m.content.length > lastLen) {
      growth++
      lastLen = m.content.length
    }
    samples.push(m.content.length)
  }
}, 20)

await send('请用大约八十字介绍你能做什么，并说明你思考时的习惯。')
await new Promise((r) => setTimeout(r, 800))
clearInterval(timer)

const m = messages.value[1]
console.log('=== 前端端到端结果 ===')
console.log('isStreaming        =', isStreaming.value)
console.log('正文长度            =', m.content.length)
console.log('思维链长度          =', (m.reasoning ?? '').length)
console.log('正文增长采样次数    =', growth, growth > 3 ? '✅ 流式逐字更新' : '❌ 冻结')
console.log('被截断              =', m.truncated ?? false)
console.log('长度采样轨迹(前12)  =', samples.slice(0, 12).join(','))
console.log('正文前 100 字       =', m.content.slice(0, 100))
console.log('正文后 100 字       =', m.content.slice(-100))

// ================= 余额（真实后端） =================
const { balance, error: balanceError, refresh: refreshBalance } = useBalance()
await refreshBalance()
console.log()
console.log('=== 余额（真实后端 /api/balance）===')
console.log('错误信息   =', balanceError.value || '（无）')
console.log('货币       =', balance.value?.currency)
console.log('总余额     =', balance.value?.total_balance)
console.log('赠金/充值  =', balance.value?.granted_balance, '/', balance.value?.topped_up_balance)
console.log('账户可用   =', balance.value?.is_available)
console.log(
  '判定       =',
  balance.value?.ok && balance.value.total_balance
    ? '✅ 余额已取到'
    : '❌ 余额未取到',
)
