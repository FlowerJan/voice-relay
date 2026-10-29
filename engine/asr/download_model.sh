#!/usr/bin/env bash
# 下载 Qwen3-ASR-0.6B 模型文件到 weights/，并校验 SHA256
set -e
BASE="https://www.modelscope.cn/api/v1/models/Qwen/Qwen3-ASR-0.6B/repo?Revision=master&FilePath="
# 用脚本自身所在目录，不受调用时的 cwd 影响
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HERE/weights/Qwen3-ASR-0.6B"
mkdir -p "$DEST"

FILES="model.safetensors vocab.json merges.txt config.json tokenizer_config.json chat_template.json preprocessor_config.json generation_config.json configuration.json README.md .gitattributes"

for f in $FILES; do
  echo "--- 下载 $f ---"
  # -C - 支持断点续传；-L 跟随重定向
  curl -L -C - --retry 5 --retry-delay 3 --connect-timeout 20 -o "$DEST/$f" "${BASE}${f}"
done

echo "=== 文件清单 ==="
ls -la "$DEST"

echo "=== SHA256 校验 model.safetensors ==="
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$DEST/model.safetensors"
else
  E:/python/python.exe -c "
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1],'rb') as f:
    for b in iter(lambda: f.read(1024*1024), b''):
        h.update(b)
print(h.hexdigest(), sys.argv[1])
" "$DEST/model.safetensors"
fi
echo "官方期望值: 79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea"
echo "=== DOWNLOAD_MODEL_DONE ==="
