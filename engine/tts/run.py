"""TTS 服务启动器（端口 8002）。

**不开 reload。** 这一层没有常驻模型（edge-tts 是远程合成），重载确实很便宜——
但便宜不等于该开：Windows 上 uvicorn 的 reload 会派生 spawn_main 子进程，父进程
被杀掉后子进程仍占着 8002，表现为"改了代码不生效"，而这个现象和"根本没改对"
长得一模一样，只能靠翻 netstat 才分得清。改这一层的代码不需要热重载兜底
（手动重启 8002 不到一秒），所以没必要为它承担那个坑。
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.server:app", host="127.0.0.1", port=8002, reload=False, log_level="info")
