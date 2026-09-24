"""
钱程似锦（Qiancheng）主程序入口

负责四件事：
1. 创建 FastAPI 应用（lifespan 中启动 / 停止后台自动检查任务，Phase 8）
2. 注册各模块的 API 路由（见 app/api/ 目录）
3. 托管 frontend 目录下的前端页面
"""
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# 读取项目根目录的 .env 文件（存放 API Key 等配置，该文件不会提交到 GitHub）
load_dotenv()

# 日志配置：数据源层会输出请求耗时、结果条数等信息
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import ai, automation, candidate, fund, goal, monitor, portfolio
from app.database.database import init_db
from app.services import automation_service

# 项目根目录 = app/ 的上一级；前端页面放在 frontend/ 目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

# 初始化数据库（表不存在时自动创建）
init_db()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动后台定时任务，关闭时停止

    定时任务运行在独立 asyncio 协程中，不阻塞请求处理；
    具体启停逻辑见 automation_service（可通过 .env 的 AUTO_TASK_ENABLED 关闭）。
    """
    automation_service.start_scheduler()
    yield
    await automation_service.stop_scheduler()


# 创建 FastAPI 应用
app = FastAPI(
    title="钱程似锦 Qiancheng",
    description="个人基金智能监控与分析系统（仅用于学习与数据分析，不构成投资建议）",
    version="0.1.0",
    lifespan=lifespan,
)

# 跨域配置：目前前后端由同一个服务提供，这里放开限制是为了
# 以后前端可以独立开发调试（例如用编辑器的 Live Server 打开页面）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由：每个业务模块一个 router，保持 main.py 干净
app.include_router(fund.router)
app.include_router(portfolio.router)
app.include_router(goal.router)
app.include_router(candidate.router)
app.include_router(ai.router)
app.include_router(monitor.router)
app.include_router(automation.router)


@app.get("/api/health", tags=["系统"], summary="健康检查")
def health_check():
    """确认后端正在运行"""
    return {"status": "ok", "version": "0.1.0"}


# 托管前端静态页面（index.html / css / js）
# 注意：挂载在 "/" 必须放在所有 API 路由注册之后，否则会拦截 /api 请求
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
