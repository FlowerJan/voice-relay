// 语音输入：把麦克风录到的 Blob 传给网关的 /api/asr，拿回文字。
// 只做"音频进、文字出"，不碰聊天逻辑。

export interface ASRResult {
  ok: boolean
  text: string
  language: string | null
  duration: number
  infer_seconds: number
}

/** 网关统一错误格式：{"error": {"code","message","status","trace_id"}} */
interface ErrorEnvelope {
  error?: { code?: string; message?: string; status?: number }
}

export class ASRError extends Error {
  code: string
  constructor(message: string, code = 'asr_failed') {
    super(message)
    this.code = code
    this.name = 'ASRError'
  }
}

export async function transcribe(
  blob: Blob,
  signal?: AbortSignal,
): Promise<ASRResult> {
  const form = new FormData()
  // 文件名只是给后端判断容器类型用（webm / mp4），内容才是真的
  const ext = (blob.type.split('/')[1] || 'webm').split(';')[0]
  form.append('file', blob, `audio.${ext}`)

  let resp: Response
  try {
    resp = await fetch('/api/asr', { method: 'POST', body: form, signal })
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e
    throw new ASRError('网络连接失败，无法上传音频', 'network')
  }

  let data: unknown = null
  try {
    data = await resp.json()
  } catch {
    throw new ASRError(`识别服务返回异常（HTTP ${resp.status}）`, 'bad_response')
  }

  if (!resp.ok) {
    const err = (data as ErrorEnvelope)?.error
    throw new ASRError(err?.message || `识别失败（HTTP ${resp.status}）`, err?.code || 'http_error')
  }

  return data as ASRResult
}
