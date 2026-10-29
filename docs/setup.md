# 安装补充：两个不进 git 的大件

这两个文件体积过大（64MB + 1.75GB），**没有进 git**。换机器或重新克隆后必须手动准备，
否则对应服务起不来。

---

## 1. ffmpeg → `engine/asr/bin/ffmpeg.exe`

浏览器录出来的是 **webm(Opus)**，ASR 侧的 `soundfile` 不支持 webm，必须先转码。

程序**只认这一个路径，不查 PATH** —— 缺了会立刻报错，不会出现"能跑但结果不对"。

```bash
cd engine/asr

# 方式一：npmmirror 镜像（国内快，约 64MB）
curl -L -o _ff.tgz "https://registry.npmmirror.com/@ffmpeg-installer/win32-x64/-/win32-x64-4.1.0.tgz"
python -c "import tarfile;t=tarfile.open('_ff.tgz');[t.extract(m,'bin') for m in t.getmembers() if m.name.endswith('ffmpeg.exe')]"
mv bin/package/ffmpeg.exe bin/ffmpeg.exe

# 方式二：官方静态包
# https://www.gyan.dev/ffmpeg/builds/  解压后只取 bin/ffmpeg.exe
```

验证：

```bash
engine/asr/bin/ffmpeg.exe -version
```

> 为什么不走官方源：官方 GitHub release 在国内会走代理，实测 7 分钟只下了 6MB / 114MB。
> npmmirror 的 `@ffmpeg-installer` 包是同一个二进制，5.8MB/s。

---

## 2. 模型权重 → `engine/asr/weights/Qwen3-ASR-0.6B/`

11 个文件、合计 1.75 GB。**文件平铺，不要再套一层目录**（代码按固定路径加载）。

```bash
cd engine/asr/weights
BASE="https://www.modelscope.cn/api/v1/models/Qwen/Qwen3-ASR-0.6B/repo?Revision=master&FilePath="
for f in model.safetensors vocab.json merges.txt config.json tokenizer_config.json \
         chat_template.json preprocessor_config.json generation_config.json configuration.json; do
  curl -L -C - -o "Qwen3-ASR-0.6B/$f" "${BASE}${f}"
done
```

`-C -` 支持断点续传，网络中断后重跑同一条命令即可。

**完整性校验**（唯一可靠的判断标准）：

```
model.safetensors 的 SHA256 应为
79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea
```

```bash
cd engine/asr/weights/Qwen3-ASR-0.6B && sha256sum model.safetensors
```

不一致就删掉重下。文件大小对但哈希不对的情况真实存在（下载被中间劫持/截断），
表现出来是模型能加载但识别结果乱码。

---

## 装完之后

```bash
E:/python/python.exe dev.py --check   # 体检：venv、端口、大件是否齐
E:/python/python.exe dev.py           # 启动
```

`--check` 会直接告诉你哪一项缺，不用逐个去猜。
