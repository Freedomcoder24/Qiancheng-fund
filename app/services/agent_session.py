"""Agent 轻量级内存会话存储（Phase 18）

设计原则（按用户要求：简单可靠、不持久化）：
- 进程内 dict 存储，服务重启即清空（对话上下文本就允许丢失）；
- 每个会话只保留最近 MAX_MESSAGES 条消息（user/assistant 文本交替，
  不含工具调用中间产物——工具结果已反映在最终回答里，控制上下文长度）；
- 超过 TTL 未交互的会话自动过期，会话总数超上限时淘汰最久未访问的；
- 未知的 session_id 一律静默新建（响应携带新 id，客户端随后使用新 id）。

只服务 Agent 对话这一处，不做通用缓存抽象。
"""
import threading
import time
import uuid

# 每会话保留的历史消息上限（8 条 = 4 轮问答），防止上下文无限增长
MAX_MESSAGES = 8
# 会话空闲过期时间（秒）：30 分钟无交互即丢弃
SESSION_TTL_SECONDS = 1800
# 会话总数上限（单用户本地系统，防极端情况内存膨胀）
MAX_SESSIONS = 200

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def _evict_expiredLocked() -> None:
    """清理过期会话 + 超上限淘汰最久未访问的（调用方需持有锁）"""
    now = time.time()
    expired = [
        sid for sid, s in _sessions.items()
        if now - s["last_access"] > SESSION_TTL_SECONDS
    ]
    for sid in expired:
        _sessions.pop(sid, None)

    if len(_sessions) >= MAX_SESSIONS:
        ordered = sorted(_sessions.items(), key=lambda kv: kv[1]["last_access"])
        for sid, _ in ordered[: len(_sessions) - MAX_SESSIONS + 1]:
            _sessions.pop(sid, None)


def get_or_create(session_id: str | None) -> tuple[str, list[dict]]:
    """取回会话历史（副本）；session_id 不存在 / 为空则新建

    返回 (session_id, history_messages)。历史为 user/assistant 消息列表，
    调用方应置于 system 之后、最新 user 消息之前。
    """
    with _lock:
        _evict_expiredLocked()
        if session_id and session_id in _sessions:
            session = _sessions[session_id]
            session["last_access"] = time.time()
            return session_id, list(session["messages"])

        new_sid = uuid.uuid4().hex
        _sessions[new_sid] = {"messages": [], "last_access": time.time()}
        return new_sid, []


def append_exchange(session_id: str, user_text: str, answer_text: str) -> None:
    """把一轮成功的问答（用户原话 + 通过红线检查的最终回答）记入会话

    只在整轮成功后调用：被红线拒绝 / 上游失败 / 超轮数的回答不入历史。
    超过 MAX_MESSAGES 时只保留最近的消息。
    """
    with _lock:
        session = _sessions.get(session_id)
        if session is None:
            return
        session["messages"] += [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": answer_text},
        ]
        if len(session["messages"]) > MAX_MESSAGES:
            session["messages"] = session["messages"][-MAX_MESSAGES:]
        session["last_access"] = time.time()


def clear_session(session_id: str) -> None:
    """删除指定会话（id 不存在时静默）"""
    with _lock:
        _sessions.pop(session_id, None)


def session_count() -> int:
    """当前存活会话数（测试 / 运维观察用）"""
    with _lock:
        return len(_sessions)
