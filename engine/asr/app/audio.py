"""音频预处理：任意格式 → 16kHz 单声道 wav。

**为什么要这一步：** 浏览器 MediaRecorder 在 Chrome/Edge 上产出 webm(Opus)，
Safari 上产 mp4。而 ASR 侧的 soundfile/libsndfile 支持 WAV/OGG/MP3/FLAC，**唯独不含 WEBM**。
所以格式问题统一在后端一处解决，前端只管上传原始 Blob。

**ffmpeg 只认一个路径**：`engine/asr/bin/ffmpeg.exe`，不查 PATH。
换机器时把项目拷走就能跑，不依赖对方机器装没装 ffmpeg。
缺文件就直接报错——环境依赖就应该"少一个立刻起不来"。
"""
import subprocess
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FFMPEG = BASE_DIR / "bin" / "ffmpeg.exe"

TARGET_SR = 16000


class AudioError(RuntimeError):
    """音频处理失败（ffmpeg 缺失 / 转码失败 / 音频为空）。"""


def ensure_ffmpeg() -> Path:
    if not FFMPEG.exists():
        raise AudioError(
            f"缺少 ffmpeg：{FFMPEG} 不存在。"
            "请下载官方静态版 ffmpeg.exe 放到 engine/asr/bin/ 下（只需这一个文件）。"
        )
    return FFMPEG


def to_wav16k(src_path: str | Path, dst_path: str | Path) -> Path:
    """把任意音频转成 16kHz 单声道 16bit PCM wav。

    -vn        丢掉视频轨（webm 里常混着视频轨）
    -ac 1      单声道
    -ar 16000  16kHz（Qwen3-ASR 的期望采样率）
    -y         覆盖输出
    """
    exe = ensure_ffmpeg()
    cmd = [
        str(exe), "-hide_banner", "-loglevel", "error",
        "-i", str(src_path),
        "-vn", "-ac", "1", "-ar", str(TARGET_SR),
        "-acodec", "pcm_s16le",
        "-y", str(dst_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not Path(dst_path).exists():
        raise AudioError(f"ffmpeg 转码失败：{(proc.stderr or '')[:300]}")
    return Path(dst_path)


def duration_seconds(wav_path: str | Path) -> float:
    """读 wav 头算时长。用 soundfile，避免再去解 ffmpeg 的日志。"""
    import soundfile as sf

    info = sf.info(str(wav_path))
    return float(info.frames) / float(info.samplerate) if info.samplerate else 0.0


def transcode_bytes(data: bytes, suffix: str = ".webm") -> tuple[Path, Path]:
    """原始字节 → 临时 webm + 转好的 wav。返回两个临时路径，调用方负责清理。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="asr_"))
    src = tmp_dir / f"input{suffix}"
    dst = tmp_dir / "audio16k.wav"
    src.write_bytes(data)
    to_wav16k(src, dst)
    return src, dst


def cleanup(*paths: str | Path) -> None:
    """删除临时文件及其所在目录（尽力而为，不抛异常）。"""
    for p in paths:
        try:
            p = Path(p)
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                for child in p.iterdir():
                    child.unlink(missing_ok=True)
                p.rmdir()
        except Exception:  # noqa: BLE001
            pass
