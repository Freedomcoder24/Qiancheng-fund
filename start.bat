@echo off
chcp 65001 >nul
title 钱程似锦 Qiancheng

:: 切换到本文件所在目录（双击运行时保证路径正确）
cd /d "%~dp0"

:: 检查虚拟环境是否存在，不存在则提示如何安装
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境 .venv，请先在项目目录执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo 正在启动 钱程似锦 Qiancheng ...
echo 停止服务：按 Ctrl+C 或直接关闭本窗口
echo.

:: 3 秒后自动打开浏览器（子进程等待，不影响服务启动）
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"

:: 启动后端服务（--reload：修改代码后自动重启，开发阶段保留）
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload


:: 服务退出后暂停窗口，方便查看报错信息
pause
