// 语音输出：把整段回答交给网关的 /api/tts，拿回 mp3 Blob。
// 整段合成，不做边生成边播（见设计文档 5.4）。

interface TTSErrorEnvelope {
  error?: { code?: string; message?: string; hint?: string }
}

export class TTSError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'TTSError'
  }
}

export async function synthesize(text: string, signal?: AbortSignal): Promise<Blob> {
  let resp: Response
  try {
    resp = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
      signal,
    })
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e
    throw new TTSError('网络连接失败，无法合成语音')
  }

  if (!resp.ok) {
    let message = `语音合成失败（HTTP ${resp.status}）`
    try {
      const data = (await resp.json()) as TTSErrorEnvelope
      const err = data?.error
      if (err?.message) message = err.hint ? `${err.message}。${err.hint}` : err.message
    } catch {
      // 忽略：不是 JSON 就用上面的默认文案
    }
    throw new TTSError(message)
  }

  return await resp.blob()
}
