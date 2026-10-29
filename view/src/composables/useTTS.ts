// 语音输出：回答完成后按句切段，边合成边播。
//
// 为什么不再整段合成：2000 字的回答要等全部合成完（好几秒）才出第一个音，
// 长回答的用户体感是"点了没反应"。切段之后第一段 300ms 就能出声，
// 后面的段在播前一段时已经在合成（预取），段间基本听不出停顿。
import { onBeforeUnmount, ref, watch } from 'vue'
import { synthesize } from '../api/tts'

const STORAGE_KEY = 'tts-enabled'
/** 单段目标长度：太短请求次数暴增（每段都要一次远程合成），太长首音延迟变大。 */
const MAX_CHUNK = 80

function readEnabled(): boolean {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v === null ? true : v === '1' // 默认开
  } catch {
    return true
  }
}

/** 按句末标点切分，再把短句合并到 MAX_CHUNK 以内。 */
export function splitChunks(text: string): string[] {
  // 围栏代码块整块丢掉：后端 clean_text 也会丢，但在这里先丢能保证
  // 切段不会切在代码中间，留下半截代码被念出来。
  const body = text.replace(/```[\s\S]*?```/g, '。')

  const sentences = body
    .replace(/\r/g, '')
    .split(/(?<=[。！？!?；;：:\n])/)
    .map((s) => s.trim())
    .filter(Boolean)

  const out: string[] = []
  let buf = ''
  for (const s of sentences) {
    if (s.length > MAX_CHUNK) {
      // 超长句（英文长句、无标点长文）先独立硬切
      if (buf) {
        out.push(buf)
        buf = ''
      }
      for (let i = 0; i < s.length; i += MAX_CHUNK) out.push(s.slice(i, i + MAX_CHUNK))
      continue
    }
    if (buf && buf.length + s.length > MAX_CHUNK) {
      out.push(buf)
      buf = s
    } else {
      buf += s
    }
  }
  if (buf) out.push(buf)
  // 纯标点段没有朗读价值（代码块被替换成句号后会留下这种段）
  return out.filter((s) => /[\p{L}\p{N}]/u.test(s))
}

export function useTTS() {
  const enabled = ref(readEnabled())
  const speaking = ref(false)
  const loading = ref(false)
  const error = ref('')
  /** 当前播到第几段（0 起），用于顶栏显示进度 */
  const index = ref(0)
  const total = ref(0)

  let audio: HTMLAudioElement | null = null
  let controller: AbortController | null = null
  /** 每次 speak/stop 自增；异步循环靠它判断"我这一轮是否已被取消" */
  let session = 0

  watch(enabled, (v) => {
    try {
      localStorage.setItem(STORAGE_KEY, v ? '1' : '0')
    } catch {
      // 隐私模式下 localStorage 可能不可用，忽略
    }
    if (!v) stop()
  })

  function cleanupAudio() {
    if (!audio) return
    audio.onended = null
    audio.onerror = null
    audio.pause()
    audio = null
  }

  function stop() {
    session++
    controller?.abort()
    controller = null
    cleanupAudio()
    speaking.value = false
    loading.value = false
    index.value = 0
    total.value = 0
  }

  function play(blob: Blob): Promise<void> {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(blob)
      const el = new Audio(url)
      audio = el
      let settled = false
      const done = () => {
        if (settled) return
        settled = true
        URL.revokeObjectURL(url)
        if (audio === el) audio = null
        resolve()
      }
      el.onended = done
      el.onerror = done
      el.play().catch(done)
    })
  }

  /** 给预取的 Promise 挂一个空 catch：本轮被取消时它可能永远没人 await。 */
  function keep(p: Promise<Blob>): Promise<Blob> {
    p.catch(() => undefined)
    return p
  }

  async function speak(text: string) {
    if (!enabled.value) return
    const chunks = splitChunks(text)
    if (!chunks.length) return

    stop()
    const my = ++session
    controller = new AbortController()
    const signal = controller.signal

    total.value = chunks.length
    index.value = 0
    speaking.value = true
    loading.value = true
    error.value = ''

    try {
      let prefetch: Promise<Blob> | null = null
      for (let i = 0; i < chunks.length; i++) {
        if (my !== session) return
        index.value = i

        const current = prefetch ?? synthesize(chunks[i], signal)
        // 一边播当前段，一边合成下一段
        prefetch = i + 1 < chunks.length ? keep(synthesize(chunks[i + 1], signal)) : null

        const blob = await current
        if (my !== session) return

        loading.value = false
        await play(blob)
      }

      if (my === session) {
        speaking.value = false
        index.value = 0
        total.value = 0
      }
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError' && my === session) {
        error.value = (e as Error).message || '语音合成失败'
        speaking.value = false
        index.value = 0
        total.value = 0
      }
    } finally {
      if (my === session) {
        controller = null
        loading.value = false
      }
    }
  }

  function toggle() {
    enabled.value = !enabled.value
  }

  onBeforeUnmount(stop)

  return { enabled, speaking, loading, error, index, total, speak, stop, toggle }
}
