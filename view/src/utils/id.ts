/**
 * 唯一 ID 生成。
 *
 * 为什么不用 `crypto.randomUUID()` 直接调用：它只在**安全上下文**下存在。
 * localhost 和 https 算安全上下文，但 `http://192.168.1.5:5173` 不算 ——
 * 一旦有人通过局域网 IP 打开页面，`crypto.randomUUID` 就是 undefined，
 * 直接调会抛 TypeError，整个会话层起不来。所以这里必须留降级分支。
 */
export function uid(prefix = 'id'): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c && typeof c.randomUUID === 'function') return `${prefix}_${c.randomUUID()}`
  // 降级：时间戳 + 随机数，同一毫秒内碰撞概率极低，本地单人使用足够
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`
}
