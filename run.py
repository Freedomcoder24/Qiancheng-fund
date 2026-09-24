"""
钱程似锦（Qiancheng）一键启动脚本

用法（任选其一）：
1. 在 Trae / VSCode 里：右键本文件 -> Run Python File
2. 终端：python run.py

运行后服务启动，浏览器会自动打开 http://127.0.0.1:8000
"""
import threading
import webbrowser

import uvicorn

URL = "http://127.0.0.1:8000"

if __name__ == "__main__":
    print(f"正在启动 钱程似锦（Qiancheng），浏览器将自动打开 {URL}")

    # 1.5 秒后自动打开浏览器（给服务启动留一点时间）
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()

    # reload=True：修改代码后服务自动重启，开发阶段很方便
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
