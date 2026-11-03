/**
 * 网关错误体的唯一解析入口。
 *
 * 后端形状固定：`{ error: { code, message, hint, status, trace_id } }`。
 * message 回答"哪里坏了"，hint 回答"现在能做什么"——两个字段分开，
 * 是因为它们的使用场合不同：日志要前者，用户要后者。
 *
 * 这里的职责只有一个：把响应变成 `ErrorInfo`，**不改写 message**。
 * 以前前端在 `!resp.ok` 时只写 `请求失败：${status}`，等于把后端写好的人话
 * （"聊天服务（8000）未启动或无法连接"）整个丢掉，用户只能看到一个数字。
 */

export interface ErrorInfo {
  /** 哪里出了问题。可以直接展示给用户。 */
  message: string
  /** 现在能做什么。缺失表示后端没给建议，界面就不要硬编一句。 */
  hint?: string
  /** 后端错误码，排障时看它比看文案准。 */
  code?: string
  status: number
}

interface ErrorEnvelope {
  error?: { code?: unknown; message?: unknown; hint?: unknown }
  /** 抽取接口成功但 `ok:false` 时也带 message，这里一并兼容。 */
  message?: unknown
}

/** 非网关返回的响应（反代 502 的 HTML 页）统一用这一句，别猜原因。 */
const NOT_OURS_HINT = '确认网关（9000）在运行；刷新页面后再试'

function textOf(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

export function parseErrorBody(data: unknown, status: number): ErrorInfo {
  const env = (data && typeof data === 'object' ? data : {}) as ErrorEnvelope
  const err = env.error
  const message =
    textOf(err?.message) || textOf(env.message) || `请求失败（HTTP ${status}）`
  const info: ErrorInfo = { message, status }
  const hint = textOf(err?.hint)
  if (hint) info.hint = hint
  const code = textOf(err?.code)
  if (code) info.code = code
  return info
}

/**
 * 从失败的响应里读出可展示的错误。
 *
 * 分两种情况，因为它们对用户的含义完全不同：
 * - JSON 且是统一错误体 → 后端知道发生了什么，照它说的展示；
 * - 不是 JSON → 对面根本不是我们的服务（典型是反代或静态服务器的 502 页面），
 *   这时不能把 HTML 塞给用户，也不能假装是后端拒绝。
 */
export async function readApiError(resp: Response): Promise<ErrorInfo> {
  let raw = ''
  try {
    raw = await resp.text()
  } catch {
    // 读 body 都失败：连接在中途断了
    return { message: '读取响应时连接中断', status: resp.status, hint: '重试一次' }
  }

  if (!raw.trim()) {
    return { message: `请求失败（HTTP ${resp.status}）`, status: resp.status, hint: NOT_OURS_HINT }
  }

  try {
    return parseErrorBody(JSON.parse(raw), resp.status)
  } catch {
    return {
      message: `服务返回了非预期的响应（HTTP ${resp.status}）`,
      status: resp.status,
      hint: NOT_OURS_HINT,
    }
  }
}

/** 单行场合（语音识别提示、播报 toast）用：把建议并进同一句话。 */
export function formatError(info: ErrorInfo): string {
  return info.hint ? `${info.message}。${info.hint}` : info.message
}
