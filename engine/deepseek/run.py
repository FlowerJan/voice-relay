"""聊天服务启动器：双击即可起，不用记 uvicorn 参数。

端口 8000 是这一层的固定身份，看端口就知道该进哪个目录看日志。

**不开 reload。** 这一层确实没有常驻模型（只加载 openai 客户端），重载成本近乎为零，
所以曾经是四个后端里唯一开着 reload 的一个。关掉的理由和 TTS 那条完全一样：
Windows 上 uvicorn 的 reload 会派生 spawn_main 子进程，父进程被杀掉后子进程仍占着
8000，表现成"改了代码不生效"——而这个现象和"根本没改对"长得一模一样，只能翻
netstat 才分得清。这一层改得最勤，恰恰意味着踩到这个坑的概率最高。
重启 8000 不到一秒，不值得为省那一秒去承担一个会伪装成"代码写错了"的故障。
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )
