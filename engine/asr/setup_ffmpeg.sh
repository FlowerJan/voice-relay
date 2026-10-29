#!/usr/bin/env bash
# 下载 ffmpeg 并只取 ffmpeg.exe 放到 bin/ 下（只认这一个路径，不查 PATH）
#
# 两个源都在用，国内优先第一个：
#   1) npmmirror 的 @ffmpeg-installer/win32-x64 —— 64MB，5~6MB/s
#   2) 官方 gyan.dev —— 114MB，走代理时常只有 15KB/s（实测 7 分钟才下 6MB）
set -e
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p bin

echo "=== 从 npmmirror 下载 ffmpeg 二进制 ==="
curl -L --retry 3 --connect-timeout 20 \
  -o _ff.tgz \
  "https://registry.npmmirror.com/@ffmpeg-installer/win32-x64/-/win32-x64-4.1.0.tgz"
ls -la _ff.tgz

echo "=== 解出 ffmpeg.exe ==="
E:/python/python.exe -c "
import tarfile
with tarfile.open('_ff.tgz') as t:
    for m in t.getmembers():
        if m.name.replace('\\\\','/').endswith('ffmpeg.exe'):
            m.name = 'ffmpeg.exe'
            t.extract(m, 'bin')
            print('已解出 -> bin/ffmpeg.exe', m.size, 'bytes')
            break
    else:
        raise SystemExit('压缩包里没找到 ffmpeg.exe')
"
rm -f _ff.tgz

echo "=== 验证 ==="
./bin/ffmpeg.exe -version 2>&1 | head -2
echo "=== SETUP_FFMPEG_DONE ==="
