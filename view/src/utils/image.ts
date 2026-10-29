/**
 * 图片压缩：把用户拖进来/粘贴的原图缩到合适尺寸。
 *
 * 为什么要压：
 *  - 一张手机截图动辄 2~4MB，base64 之后还多 33%。不压的话一次请求就是十几 MB，
 *    既慢又容易触发上游的体积限制（400 会表现为"请求被拒绝"）。
 *  - 实测一张 20KB 的 PNG 截图，prompt 只算 250 tokens 左右；图片不是主要成本，
 *    但体积会直接影响上传耗时。
 *
 * 两档输出：
 *  - dataUrl：长边 ≤1400，给模型看
 *  - thumb：长边 ≤320，给界面显示、也随 localStorage 一起存（10KB 量级）
 */
import { uid } from './id'

export const MAX_EDGE = 1400
export const THUMB_EDGE = 320
const QUALITY = 0.78
const THUMB_QUALITY = 0.6

/** 单张原图上限：再大就先提示，避免一次卡住主线程十几秒 */
export const MAX_INPUT_BYTES = 20 * 1024 * 1024

export const ACCEPT = 'image/png,image/jpeg,image/webp,image/gif'

export interface LoadedImage {
  id: string
  /** 长边 ≤1400 的 JPEG dataURL，发给模型用 */
  dataUrl: string
  /** 长边 ≤320 的 JPEG dataURL，界面显示 + 持久化用 */
  thumb: string
  w: number
  h: number
  name: string
}

function loadElement(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('图片解码失败'))
    img.src = src
  })
}

function drawToDataUrl(img: HTMLImageElement, maxEdge: number, quality: number): string {
  const scale = Math.min(1, maxEdge / Math.max(img.naturalWidth, img.naturalHeight))
  const w = Math.max(1, Math.round(img.naturalWidth * scale))
  const h = Math.max(1, Math.round(img.naturalHeight * scale))

  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('当前浏览器不支持 canvas')

  // 统一铺白底：PNG 透明区域转 JPEG 会变黑，截图带透明背景时非常难看
  ctx.fillStyle = '#fff'
  ctx.fillRect(0, 0, w, h)
  ctx.drawImage(img, 0, 0, w, h)
  return canvas.toDataURL('image/jpeg', quality)
}

/** 读文件 → 压两档。抛出的错误文案直接可展示给用户。 */
export async function loadImageFile(file: File): Promise<LoadedImage> {
  if (!file.type.startsWith('image/')) throw new Error('只能发送图片文件')
  if (file.size > MAX_INPUT_BYTES) throw new Error('图片超过 20MB，请先压缩或裁剪')

  const objectUrl = URL.createObjectURL(file)
  try {
    const img = await loadElement(objectUrl)
    const dataUrl = drawToDataUrl(img, MAX_EDGE, QUALITY)
    const thumb = drawToDataUrl(img, THUMB_EDGE, THUMB_QUALITY)
    return {
      id: uid('img'),
      dataUrl,
      thumb,
      w: img.naturalWidth,
      h: img.naturalHeight,
      name: file.name || '粘贴的图片',
    }
  } finally {
    URL.revokeObjectURL(objectUrl)
  }
}

/** 从剪贴板/拖拽里挑出图片文件（截图工具、微信截图都走这条路）。 */
export function pickImageFiles(items: DataTransfer | null): File[] {
  if (!items) return []
  const out: File[] = []
  for (const item of Array.from(items.items ?? [])) {
    if (item.kind !== 'file') continue
    const f = item.getAsFile()
    if (f && f.type.startsWith('image/')) out.push(f)
  }
  if (out.length) return out
  for (const f of Array.from(items.files ?? [])) {
    if (f.type.startsWith('image/')) out.push(f)
  }
  return out
}
