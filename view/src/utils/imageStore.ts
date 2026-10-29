/**
 * 原图存储：IndexedDB。
 *
 * 为什么不用 localStorage：它只有 ~5MB，而一张 1400px 的截图压缩后仍有
 * 80~150KB。塞进 localStorage 的话，二三十张图就会触发"写满降级"，
 * 连带把文字历史裁掉——那是数据丢失，不是降级。
 *
 * 分工：
 *  - 消息里只存 id + 小缩略图（thumb，约 10KB），跟着 localStorage 走，刷新即可显示
 *  - 原图（发给模型、放大查看用）存这里，按需读取
 *
 * 降级：隐私模式 / 禁用 IndexedDB 时退化为进程内 Map。此时刷新后原图丢失，
 * 但缩略图和文字都还在，对话仍然可读——比整个功能不可用要好。
 */
const DB_NAME = 'vta-images'
const STORE = 'img'
const VERSION = 1

/** 最近用过的原图放内存里，避免每次重发都去读盘。 */
const cache = new Map<string, string>()
const CACHE_MAX = 24

let dbPromise: Promise<IDBDatabase | null> | null = null

function openDb(): Promise<IDBDatabase | null> {
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve) => {
    try {
      if (typeof indexedDB === 'undefined') return resolve(null)
      const req = indexedDB.open(DB_NAME, VERSION)
      req.onupgradeneeded = () => {
        const db = req.result
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE)
      }
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => resolve(null)
      req.onblocked = () => resolve(null)
    } catch {
      resolve(null)
    }
  })
  return dbPromise
}

function remember(id: string, dataUrl: string): void {
  cache.set(id, dataUrl)
  while (cache.size > CACHE_MAX) {
    const oldest = cache.keys().next().value
    if (oldest === undefined) break
    cache.delete(oldest)
  }
}

export async function putImage(id: string, dataUrl: string): Promise<void> {
  remember(id, dataUrl)
  const db = await openDb()
  if (!db) return
  await new Promise<void>((resolve) => {
    try {
      const tx = db.transaction(STORE, 'readwrite')
      tx.objectStore(STORE).put(dataUrl, id)
      tx.oncomplete = () => resolve()
      tx.onerror = () => resolve()
      tx.onabort = () => resolve()
    } catch {
      resolve()
    }
  })
}

export async function getImage(id: string): Promise<string | null> {
  const hit = cache.get(id)
  if (hit) return hit
  const db = await openDb()
  if (!db) return null
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(STORE, 'readonly')
      const req = tx.objectStore(STORE).get(id)
      req.onsuccess = () => {
        const v = req.result
        if (typeof v === 'string' && v) {
          remember(id, v)
          resolve(v)
        } else resolve(null)
      }
      req.onerror = () => resolve(null)
    } catch {
      resolve(null)
    }
  })
}

/** 批量取：返回仍然存在的原图，缺失的直接跳过（用户可能清了浏览器数据）。 */
export async function getImages(ids: string[]): Promise<string[]> {
  const out: string[] = []
  for (const id of ids) {
    const v = await getImage(id)
    if (v) out.push(v)
  }
  return out
}

/** 删除会话时顺手清图，避免 IndexedDB 里留下永远读不到的孤儿数据。 */
export async function deleteImages(ids: string[]): Promise<void> {
  for (const id of ids) cache.delete(id)
  if (!ids.length) return
  const db = await openDb()
  if (!db) return
  await new Promise<void>((resolve) => {
    try {
      const tx = db.transaction(STORE, 'readwrite')
      const store = tx.objectStore(STORE)
      for (const id of ids) store.delete(id)
      tx.oncomplete = () => resolve()
      tx.onerror = () => resolve()
      tx.onabort = () => resolve()
    } catch {
      resolve()
    }
  })
}
