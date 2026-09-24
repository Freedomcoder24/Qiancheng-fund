"""Agent 模块（Phase 15）：基于 Tool Calling 的基金分析智能体

目录职责：
- tools.py：Tool 注册表（JSON Schema 定义 + 执行器），全部直接复用现有 service 层
- （Agent 循环在 app/services/agent_service.py，路由在 app/api/agent.py）
"""
