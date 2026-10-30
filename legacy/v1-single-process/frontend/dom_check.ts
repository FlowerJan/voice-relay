// DOM 级验证：真实挂载 ChatView（顶栏余额徽标）与 MessageList（流式渲染），
// 断言 DOM 随写入更新、思维链折叠默认收起、Markdown 正确、余额正确显示。
import { JSDOM } from 'jsdom'

// --- 必须在 import vue 之前建好 DOM 环境 ---
const dom = new JSDOM('<!DOCTYPE html><div id="app"></div>', { url: 'http://localhost:5173' })
const w = dom.window as any
globalThis.window = w
globalThis.document = w.document
// Node 22 的 globalThis.navigator 只有 getter，必须用 defineProperty 覆盖
Object.defineProperty(globalThis, 'navigator', { value: w.navigator, configurable: true, writable: true })
globalThis.HTMLElement = w.HTMLElement
globalThis.Element = w.Element
globalThis.Node = w.Node
globalThis.Text = w.Text
globalThis.SVGElement = w.SVGElement
globalThis.getComputedStyle = w.getComputedStyle.bind(w)
globalThis.requestAnimationFrame = (cb: any) => setTimeout(cb, 0)
globalThis.cancelAnimationFrame = (id: any) => clearTimeout(id)

// 打桩 fetch：余额接口返回固定数据
globalThis.fetch = (async (input: any) => {
  const url = String(input)
  if (url.includes('/api/balance')) {
    return new Response(
      JSON.stringify({
        ok: true,
        is_available: true,
        currency: 'CNY',
        total_balance: '9.87',
        granted_balance: '0.00',
        topped_up_balance: '9.87',
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )
  }
  return new Response('', { status: 200 })
}) as typeof fetch

const { createApp, h, ref, nextTick } = await import('vue')
const { default: MessageList } = await import('./src/components/MessageList.vue')
const { default: ChatView } = await import('./src/views/ChatView.vue')

// ================= 一、ChatView 顶栏余额 =================
const viewEl = document.createElement('div')
document.body.appendChild(viewEl)
createApp(ChatView).mount(viewEl)
await nextTick()
await new Promise((r) => setTimeout(r, 50)) // 等 onMounted 的余额请求落地
await nextTick()

const viewText = viewEl.textContent ?? ''
console.log('=== ChatView 顶栏余额 ===')
console.log('顶栏文本 =', JSON.stringify(viewText.trim()))
console.log('显示"余额"字样 =', viewText.includes('余额'))
console.log('显示金额 9.87   =', viewText.includes('9.87'))
console.log('带 ¥ 符号      =', viewText.includes('¥'))
console.log('有刷新按钮      =', viewEl.querySelector('.refresh') !== null)

// ================= 二、MessageList 流式渲染 =================
const messages = ref<any[]>([
  { role: 'user', content: '你好' },
  { role: 'assistant', content: '', reasoning: '' },
])

const Root = {
  setup: () => ({ messages }),
  render: () => h(MessageList, { messages: messages.value, thinking: false }),
}

const el = document.createElement('div')
document.body.appendChild(el)
createApp(Root).mount(el)

const text = () => el.textContent ?? ''

console.log()
console.log('=== MessageList DOM 渲染验证 ===')

messages.value[1].reasoning = '我在思考这个问题……'
await nextTick()
console.log('[写入思维链] 含"思考过程" =', text().includes('思考过程'))
console.log('           含 details 标签 =', el.querySelector('details') !== null)
console.log('           details 默认收起 =', (el.querySelector('details') as any)?.open === false)

messages.value[1].content = '第一段'
await nextTick()
console.log('[写入正文1] DOM 含"第一段" =', text().includes('第一段'))

messages.value[1].content += '，第二段'
await nextTick()
console.log('[写入正文2] DOM 含"第二段" =', text().includes('第二段'))

messages.value[1].content += '\n\n- 要点甲\n- 要点乙\n\n**加粗**'
await nextTick()
console.log('[Markdown] 生成 <ul> =', el.querySelector('ul') !== null)
console.log('         生成 <strong> =', el.querySelector('strong') !== null)
console.log('         生成 <li> 数量 =', el.querySelectorAll('li').length)

messages.value[1].truncated = true
await nextTick()
console.log('[截断标记] DOM 含提示 =', text().includes('被截断'))

console.log()
console.log('=== MessageList 完整 DOM 文本 ===')
console.log(text().trim())

// ================= 三、余额错误态 =================
globalThis.fetch = (async () =>
  new Response(JSON.stringify({ ok: false, message: 'API 密钥无效' }), {
    status: 401,
    headers: { 'Content-Type': 'application/json' },
  })) as typeof fetch

const errEl = document.createElement('div')
document.body.appendChild(errEl)
createApp(ChatView).mount(errEl)
await nextTick()
await new Promise((r) => setTimeout(r, 50))
await nextTick()

console.log()
console.log('=== 余额错误态 ===')
console.log('显示"余额获取失败" =', (errEl.textContent ?? '').includes('余额获取失败'))
console.log('title 带失败原因   =', (errEl.querySelector('.err') as any)?.getAttribute('title'))
