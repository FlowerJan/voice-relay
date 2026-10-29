// 录音：点一下开始，再点一下结束；到 60 秒自动停。
// 状态机：idle → recording → transcribing → idle（或 error）
//
// 三个边界必须处理（不处理就出问题）：
//   1. 首次点按钮浏览器弹麦克风授权 → 被拒要明确提示，不能静默失败
//   2. 一句话没说就点结束 → 短于 0.5 秒直接丢弃，不发请求
//   3. 说超过 60 秒 → 自动停止并上传
import { computed, onBeforeUnmount, ref } from 'vue'
import { transcribe } from '../api/asr'

export type RecorderState = 'idle' | 'recording' | 'transcribing' | 'error'

export const MAX_SECONDS = 60

function pickMimeType(): string {
  if (typeof MediaRecorder === 'undefined') return ''
  // Chrome/Edge 产 webm(Opus)，Safari 产 mp4；后端统一用 ffmpeg 转 16k wav
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ]
  for (const c of candidates) {
    if (MediaRecorder.isTypeSupported(c)) return c
  }
  return ''
}

export function useRecorder() {
  const state = ref<RecorderState>('idle')
  const seconds = ref(0)
  const error = ref('')

  let recorder: MediaRecorder | null = null
  let stream: MediaStream | null = null
  let chunks: Blob[] = []
  let startedAt = 0
  let timer: number | null = null

  const isSupported = computed(
    () => typeof navigator !== 'undefined' && !!navigator.mediaDevices && typeof MediaRecorder !== 'undefined',
  )
  const isRecording = computed(() => state.value === 'recording')
  const isBusy = computed(() => state.value === 'transcribing')
  const label = computed(() =>
    state.value === 'transcribing' ? '识别中' : String(seconds.value).padStart(2, '0'),
  )

  function clearTimer() {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
  }

  function releaseStream() {
    stream?.getTracks().forEach((t) => t.stop())
    stream = null
  }

  function stopRecorder(): Promise<Blob> {
    return new Promise((resolve) => {
      const rec = recorder
      const type = rec?.mimeType || 'audio/webm'
      if (!rec || rec.state === 'inactive') {
        resolve(new Blob(chunks, { type }))
        return
      }
      rec.onstop = () => resolve(new Blob(chunks, { type }))
      rec.stop()
    })
  }

  async function start() {
    if (state.value === 'recording' || state.value === 'transcribing') return
    if (!isSupported.value) {
      error.value = '当前浏览器不支持录音（需要 https 或 localhost）'
      state.value = 'error'
      return
    }

    error.value = ''
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      })
    } catch (e) {
      const name = (e as Error)?.name ?? ''
      error.value =
        name === 'NotAllowedError' || name === 'SecurityError'
          ? '未获得麦克风权限，请在浏览器地址栏左侧允许麦克风访问'
          : `无法打开麦克风：${name || '未知错误'}`
      state.value = 'error'
      return
    }

    chunks = []
    const mime = pickMimeType()
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream)
    } catch {
      error.value = '无法初始化录音器'
      releaseStream()
      state.value = 'error'
      return
    }
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data)
    }
    // 每 200ms 切一个 chunk：最后一段不会因为 stop 太快而丢
    recorder.start(200)

    startedAt = Date.now()
    seconds.value = 0
    state.value = 'recording'
    timer = window.setInterval(() => {
      seconds.value = Math.floor((Date.now() - startedAt) / 1000)
      if (seconds.value >= MAX_SECONDS) void stopAndTranscribe()
    }, 250)
  }

  /** 结束录音并识别。返回识别出的文字；空字符串表示丢弃或失败。 */
  async function stopAndTranscribe(): Promise<string> {
    if (state.value !== 'recording') return ''
    clearTimer()

    const elapsed = Date.now() - startedAt
    const blob = await stopRecorder()
    releaseStream()
    recorder = null

    // 误触：短于 0.5 秒或体积过小，直接丢弃
    if (elapsed < 500 || blob.size < 1200) {
      state.value = 'idle'
      seconds.value = 0
      return ''
    }

    state.value = 'transcribing'
    try {
      const r = await transcribe(blob)
      state.value = 'idle'
      seconds.value = 0
      return r.text
    } catch (e) {
      error.value = (e as Error).message || '识别失败'
      state.value = 'error'
      return ''
    }
  }

  /** 放弃当前录音，不上传。 */
  function cancel() {
    clearTimer()
    if (recorder && recorder.state !== 'inactive') recorder.stop()
    releaseStream()
    recorder = null
    chunks = []
    seconds.value = 0
    state.value = 'idle'
  }

  function clearError() {
    if (state.value === 'error') state.value = 'idle'
    error.value = ''
  }

  onBeforeUnmount(() => {
    clearTimer()
    releaseStream()
  })

  return {
    state,
    seconds,
    error,
    label,
    isSupported,
    isRecording,
    isBusy,
    start,
    stopAndTranscribe,
    cancel,
    clearError,
  }
}
