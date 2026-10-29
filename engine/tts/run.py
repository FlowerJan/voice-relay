"""TTS 服务启动器（端口 8002）。

这一层没有常驻模型（edge-tts 是远程合成），reload 成本很低，可以开。
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.server:app", host="127.0.0.1", port=8002, reload=True, log_level="info")
