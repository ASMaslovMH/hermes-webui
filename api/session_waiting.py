"""Беседы, которые ждут владельца, хотя живой очереди подтверждений уже нет.

Случай: команда ждала подтверждения 5 минут, очередь approval опустела, агент
ответил обычным текстом («Остановился: …»). Живые очереди пусты, и без этого
модуля беседа не попадает в «Ждут вас».

Признак: хвост транскрипта в state.db — финальное сообщение assistant (текст,
без tool_calls), перед ним подряд сообщения tool, среди которых результат с
«timed out without user response». Любое другое сообщение (новый вызов
инструмента, ответ владельца) снимает признак.
"""
from __future__ import annotations

import threading
from pathlib import Path

EXPIRED_MARKER = "timed out without user response"
_TAIL = 15
_CACHE_LIMIT = 4096

_lock = threading.Lock()
_local = threading.local()
_cache: dict[tuple[str, str], tuple[int, bool]] = {}

_LAST_ID_SQL = "SELECT MAX(id) FROM messages WHERE session_id = ?"
_TAIL_SQL = (
    "SELECT role, "
    "(tool_calls IS NOT NULL AND tool_calls NOT IN ('', '[]')) AS has_calls, "
    "instr(content, ?) > 0 AS expired, "
    "TRIM(COALESCE(substr(content, 1, 200), '')) AS head "
    "FROM messages WHERE session_id = ? AND (active IS NULL OR active = 1) "
    "ORDER BY id DESC LIMIT ?"
)


def clear() -> None:
    with _lock:
        _cache.clear()
    _drop_connection()


def _drop_connection() -> None:
    conn = getattr(_local, "conn", None)
    _local.conn = None
    _local.path = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _connection(path: Path):
    """Одно read-only соединение на поток: открытие state.db на каждую строку списка дорого."""
    from api.agent_sessions import open_state_db_readonly

    if getattr(_local, "conn", None) is not None and getattr(_local, "path", None) == str(path):
        return _local.conn
    _drop_connection()
    _local.conn = open_state_db_readonly(path)
    _local.path = str(path)
    return _local.conn


def _tail_is_expired_approval(conn, session_id: str) -> bool:
    rows = conn.execute(_TAIL_SQL, (EXPIRED_MARKER, session_id, _TAIL)).fetchall()
    if not rows:
        return False
    role, has_calls, _expired, head = rows[0]
    if role != "assistant" or has_calls or not head:
        return False
    for role, _has_calls, expired, _head in rows[1:]:
        if role != "tool":
            return False
        if expired:
            return True
    return False


def expired_approval_wait(session_id: str, db_path: Path | None) -> bool:
    """True, если беседа остановилась на истёкшем подтверждении и владелец не отвечал.

    Никогда не бросает исключений: недоступная или битая база означает «нет».
    """
    if not session_id or db_path is None:
        return False
    try:
        path = Path(db_path)
        if not path.exists():
            return False
        key = (str(path), session_id)
        try:
            conn = _connection(path)
            row = conn.execute(_LAST_ID_SQL, (session_id,)).fetchone()
            last_id = int(row[0]) if row and row[0] is not None else 0
            if not last_id:
                return False
            with _lock:
                hit = _cache.get(key)
            if hit and hit[0] == last_id:
                return hit[1]
            result = _tail_is_expired_approval(conn, session_id)
        except Exception:
            _drop_connection()  # битое соединение не держим
            raise
        with _lock:
            if len(_cache) >= _CACHE_LIMIT:
                _cache.clear()
            _cache[key] = (last_id, result)
        return result
    except Exception:
        return False
