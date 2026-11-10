"""«Ждут вас»: беседа, остановленная из-за истёкшего подтверждения.

Живой случай (сессия 4a75942b294e, 04.11): команда ждала подтверждения 5 минут,
approval-очередь опустела, агент ответил обычным текстом «Остановился: …».
`_session_attention_summary` смотрел только на живые очереди approval/clarify и
возвращал None, поэтому беседа не попадала в фильтр «Ждут вас».
"""
import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import api.models as models
import api.routes as routes

BLOCKED = json.dumps({
    "output": "",
    "exit_code": -1,
    "error": "BLOCKED: Command timed out without user response. The user has NOT consented to this action.",
    "status": "blocked",
    "user_summary": "No answer within 5 minutes — the command did not run.",
})


@pytest.fixture()
def state_db(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT, finish_reason TEXT, active INTEGER)"
    )
    conn.execute("CREATE INDEX idx_messages_session_id ON messages(session_id, id)")
    conn.commit()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: path)
    from api import session_waiting

    session_waiting.clear()  # кеш между тестами не переиспользуется
    yield conn
    conn.close()


def _add(conn, sid, role, content="", tool_calls=None, finish=None, tool_name=None):
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, finish_reason, active) "
        "VALUES (?,?,?,?,?,?,1)",
        (sid, role, content, tool_calls, tool_name, finish),
    )
    conn.commit()


def _clear(sid):
    with routes._lock:
        routes._pending.pop(sid, None)


def test_expired_approval_then_final_text_needs_user(state_db):
    sid = "wait-expired-1"
    _clear(sid)
    _add(state_db, sid, "user", "Сделай сборку")
    _add(state_db, sid, "assistant", "", tool_calls='[{"id":"c1"}]', finish="tool_calls")
    _add(state_db, sid, "tool", BLOCKED, tool_name="terminal")
    _add(state_db, sid, "assistant", "Остановился: подтверждение на удаление не пришло. Продолжить?", finish="stop")

    summary = routes._session_attention_summary(sid)

    assert summary is not None, "беседа ждёт владельца, но не видна в «Ждут вас»"
    assert summary["kind"] == "attention"
    assert summary["count"] == 1
    assert summary["reason"] == "approval_expired"


def test_agent_continued_after_expired_approval_is_not_waiting(state_db):
    sid = "wait-continued-1"
    _clear(sid)
    _add(state_db, sid, "user", "x")
    _add(state_db, sid, "tool", BLOCKED, tool_name="terminal")
    _add(state_db, sid, "assistant", "Перезапускаю другим способом", tool_calls='[{"id":"c2"}]', finish="tool_calls")
    assert routes._session_attention_summary(sid) is None


def test_owner_already_replied_is_not_waiting(state_db):
    sid = "wait-replied-1"
    _clear(sid)
    _add(state_db, sid, "tool", BLOCKED, tool_name="terminal")
    _add(state_db, sid, "assistant", "Остановился.", finish="stop")
    _add(state_db, sid, "user", "Подтверждаю")
    assert routes._session_attention_summary(sid) is None


def test_final_text_without_expired_approval_is_not_waiting(state_db):
    sid = "wait-plain-1"
    _clear(sid)
    _add(state_db, sid, "user", "привет")
    _add(state_db, sid, "tool", json.dumps({"output": "ok", "exit_code": 0}), tool_name="terminal")
    _add(state_db, sid, "assistant", "Готово.", finish="stop")
    assert routes._session_attention_summary(sid) is None


def test_live_pending_approval_still_wins(state_db):
    sid = "wait-live-1"
    _add(state_db, sid, "tool", BLOCKED, tool_name="terminal")
    _add(state_db, sid, "assistant", "Остановился.", finish="stop")
    with routes._lock:
        routes._pending[sid] = [{"approval_id": "a1", "command": "x"}]
    try:
        assert routes._session_attention_summary(sid)["kind"] == "approval"
    finally:
        _clear(sid)


def test_new_message_invalidates_cached_answer(state_db):
    sid = "wait-cache-1"
    _clear(sid)
    _add(state_db, sid, "tool", BLOCKED, tool_name="terminal")
    _add(state_db, sid, "assistant", "Остановился.", finish="stop")
    assert routes._session_attention_summary(sid) is not None
    _add(state_db, sid, "user", "Подтверждаю")
    assert routes._session_attention_summary(sid) is None


def test_missing_or_broken_state_db_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "nope.db")
    assert routes._session_attention_summary("whatever") is None
    bad = tmp_path / "bad.db"
    bad.write_text("not sqlite")
    monkeypatch.setattr(models, "_active_state_db_path", lambda: bad)
    assert routes._session_attention_summary("whatever") is None
