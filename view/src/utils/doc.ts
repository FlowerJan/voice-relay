/**
 * 文档上传与抽取。
 *
 * 分工：**解析在后端做**（engine/deepseek 的 /api/extract），前端只负责挑文件、
 * 上传、拿回纯文本。原因见后端 extract_service.py：PDF 的中文字库映射和双栏
 * 阅读顺序在浏览器端做不好，抽错了比抽不出来更糟。
 */
import { uid } from './id'

/** 单文件上限，与后端 MAX_UPLOAD_BYTES 保持一致（后端那层才是真防线）。 */
export const MAX_DOC_BYTES = 20 * 1024 * 1024

/** 一次最多带几个文档：再多的话 prompt 会爆，挑选成本也高。 */
export const MAX_DOCS = 4

export const DOC_ACCEPT = [
  '.pdf',
  '.docx',
  '.xlsx',
  '.xlsm',
  '.csv',
  '.txt',
  '.md',
  '.markdown',
  '.json',
  '.yaml',
  '.yml',
  '.xml',
  '.html',
  '.log',
  '.py',
  '.js',
  '.ts',
  '.tsx',
  '.jsx',
  '.vue',
  '.java',
  '.go',
  '.rs',
  '.sql',
  '.sh',
  '.css',
].join(',')

/**
 * 已抽取的文档。`text` 是**全文**（后端已截断到 20000 字），随消息存在 localStorage 里。
 * 不做 IndexedDB 分离：和图片不同，这里存的就是要发出去的内容，没有"原图/缩略图"两档。
 */
export interface MsgDoc {
  id: string
  name: string
  kind: string
  text: string
  chars: number
  truncated: boolean
  pages: number
  scanned: boolean
  note: string
}

interface ExtractResponse {
  ok?: boolean
  name?: string
  kind?: string
  text?: string
  chars?: number
  truncated?: boolean
  pages?: number
  scanned?: boolean
  note?: string
  message?: string
  error?: { message?: string; hint?: string }
}

/** 上传并抽取。抛出的错误文案直接可展示。 */
export async function extractDoc(file: File, signal?: AbortSignal): Promise<MsgDoc> {
  if (file.size > MAX_DOC_BYTES) {
    throw new Error(`「${file.name}」超过 20MB，请先拆分或压缩`)
  }
  const fd = new FormData()
  fd.append('file', file)

  let resp: Response
  try {
    resp = await fetch('/api/extract', { method: 'POST', body: fd, signal })
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw new Error('已取消')
    throw new Error('上传失败，检查网络或网关（9000）是否在运行')
  }

  let data: ExtractResponse = {}
  try {
    data = (await resp.json()) as ExtractResponse
  } catch {
    // 网关返回了非 JSON（比如 502 的 HTML 错误页）
    throw new Error(`解析服务返回异常（HTTP ${resp.status}）`)
  }

  if (!resp.ok || data.ok === false) {
    // 后端把"扫描件""老格式 .doc"这类可自助解决的问题都写在 message 里，直接透给用户；
    // hint 是"那我现在做什么"（做 OCR / 另存为 docx），并进去才是一句完整的话。
    const msg =
      data.message || data.error?.message || `解析失败（HTTP ${resp.status}）`
    throw new Error(data.error?.hint ? `${msg}。${data.error.hint}` : msg)
  }

  const text = data.text ?? ''
  if (!text) throw new Error('这个文件里没有可读的文字')

  return {
    id: uid('doc'),
    name: data.name || file.name,
    kind: data.kind || 'text',
    text,
    chars: data.chars ?? text.length,
    truncated: Boolean(data.truncated),
    pages: data.pages ?? 0,
    scanned: Boolean(data.scanned),
    note: data.note || '',
  }
}

/**
 * 从拖拽/粘贴数据里挑出可解析的文档。
 *
 * 用扩展名判断而不是 `file.type`：Windows 上拖进来的 PDF 经常 type 为空字符串，
 * docx 的 type 也各式各样（有些环境给 x-zip-compressed）。
 */
export function pickDocFiles(items: DataTransfer | null): File[] {
  if (!items) return []
  const ok = (name: string) => {
    const dot = name.lastIndexOf('.')
    if (dot < 0) return false
    return DOC_ACCEPT.split(',').includes(name.slice(dot).toLowerCase())
  }
  const out: File[] = []
  for (const f of Array.from(items.files ?? [])) {
    if (!f.type.startsWith('image/') && ok(f.name)) out.push(f)
  }
  return out
}

/** 展示用的体积/字数描述，例如「12 页 · 8432 字」。 */
export function docMeta(d: MsgDoc): string {
  const bits: string[] = []
  if (d.pages > 0) bits.push(`${d.pages} 页`)
  bits.push(`${d.chars} 字`)
  if (d.truncated) bits.push('已截断')
  return bits.join(' · ')
}
