#!/usr/bin/env bash
# ASR 独立环境安装。顺序不能反：
#   1) 建 venv  2) 先装 CPU 版 torch  3) qwen-asr --no-deps  4) 按真实依赖单独装
#
# ⚠️ 第 3 步的 --no-deps 是必须的：正常装会为了满足 gradio 的依赖约束
#    逐个回溯下载历史版本，白下几百 MB 还可能失败。
#
# ⚠️ 第 4 步只装**真正被 import 的**：qwen_asr 主路径只用到
#    numpy / torch / transformers / librosa / soundfile / nagisa。
#    元数据里写的 qwen-omni-utils、sox、soynlp、gradio、flask 都不在主路径上，
#    其中 qwen-omni-utils 会拖 av（需要源码编译），能绕就绕。
set -e

# 用脚本自身所在目录，不受调用时的 cwd 影响（之前这里写成 "$0/.." 多跳了一级，venv 建错地方）
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-E:/python/python.exe}"

echo "=== [1/4] 建独立 venv（Python 3.11） ==="
"$PY" -m venv venv
venv/Scripts/python.exe -m pip install --upgrade pip -q
venv/Scripts/python.exe -V

echo "=== [2/4] 先装 CPU 版 torch（顺序最关键） ==="
venv/Scripts/python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
venv/Scripts/python.exe -c "import torch; print('torch', torch.__version__, '| cuda =', torch.cuda.is_available())"

echo "=== [3/4] qwen-asr 本体（--no-deps） ==="
venv/Scripts/python.exe -m pip install qwen-asr --no-deps

echo "=== [4/4] 真实依赖 ==="
venv/Scripts/python.exe -m pip install \
  "transformers==4.57.6" "nagisa==0.2.11" librosa soundfile numpy scipy \
  fastapi uvicorn python-multipart

echo "=== 验证 ==="
venv/Scripts/python.exe -c "
import torch, transformers, qwen_asr, librosa, soundfile, numpy
print('torch        =', torch.__version__)
print('transformers =', transformers.__version__)
print('numpy        =', numpy.__version__)
print('cuda_available =', torch.cuda.is_available())
print('ALL IMPORTS OK')
"
echo "=== 下一步：bash setup_ffmpeg.sh && bash download_model.sh && venv/Scripts/python.exe smoke_test.py ==="
