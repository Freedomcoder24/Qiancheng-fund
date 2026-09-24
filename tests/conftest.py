"""pytest 全局配置

Phase 9 起后台任务在启动后数秒即执行首轮检查（不再等一个完整间隔），
为避免测试期间后台轮次写到真实开发库 / 触发真实外部请求，
这里默认对所有用例禁用 AUTO_TASK_ENABLED；
需要测试调度行为的用例（test_automation_api 等）自行 setenv 覆盖即可。
"""
import pytest


@pytest.fixture(autouse=True)
def _disable_auto_task(monkeypatch):
    monkeypatch.setenv("AUTO_TASK_ENABLED", "false")
