#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""亲子沟通军师 · 本地档案与对话记录（同意门禁 · 限量 · 可撤销）

只保存**结构化的字段**，不保存任何原话、逐字稿片段、录音或可识别信息。
启用前必须由家长明确同意（`enable --confirm`）；之后可随时查看、暂停、撤销、删除。

两条数据线：
  档案（profile）    孩子的年龄学段性别、青春期状态、家长的理念与自评、场景标签、历次等级
  对话记录（dialogue）每沟通一次记一条：日期、聊了什么、当时的沟通状态、双方感受、一个变化点
                      —— 按时间连成一条线，看关系状态的走向；不按话题归类、不做去重

用法示例：
    python archive.py status
    python archive.py enable --confirm
    python archive.py apply --json '{"field":"child_age","value":"9"}'
    python archive.py dialogue-add --json '{"summary":"聊了写作业的事","state":"卡住","parent_feeling":"有点急","child_observation":"说了两句就不说了","change_point":"这次没摔门"}'
    python archive.py dialogue-show
    python archive.py context --max-chars 4000
    python archive.py undo
    python archive.py revoke --confirm
    python archive.py clear --confirm
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POLICY_VERSION = "2"
MAX_VALUE_CHARS = 200
MAX_ROWS = 500
MAX_DIALOGUES = 400
MAX_OPERATIONS = 20

# ---- 档案字段白名单（不在表里的一律拒写）----
PROFILE_FIELDS = {
    "child_age", "child_stage", "child_sex", "puberty_stage",
    "parent_age", "parent_sex", "parent_view", "parent_self_rating",
    "scene_tags", "session_level",
}
# ---- 对话记录字段白名单 ----
# 单主体设计：家长只记自己的孩子，不传就用这一个。
DEFAULT_SUBJECT = "child"

DIALOGUE_FIELDS = {
    "dialogued_at", "summary", "state", "parent_feeling",
    "child_observation", "change_point",
}

# 沟通状态：描述档，不是评分。
STATES = {"顺畅", "还行", "有点卡", "卡住", "对抗", "回避", "没说上", "说不清"}
LEVELS = {"稳", "需调整", "需重点关注"}

# 硬性黑名单：字段名或取值里出现这些词一律拒绝——它们都指向"内容"而不是"结构"。
BLOCKLIST = (
    "原话", "逐字稿", "录音", "姓名", "名字", "学校", "班级", "生日", "出生",
    "地址", "电话", "手机号", "身份证", "诊断", "微信", "QQ",
)


class MemoryErrorWithCode(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def memory_dir() -> Path:
    override = os.environ.get("QINZIJUNSHI_MEMORY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "qinzijunshi"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return base / "qinzijunshi"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "qinzijunshi"


def db_path() -> Path:
    return memory_dir() / "memory.sqlite3"


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def connect(create: bool = False) -> sqlite3.Connection:
    target = db_path()
    if not target.exists() and not create:
        raise MemoryErrorWithCode("NOT_INITIALIZED", "尚未启用档案")
    if create:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            target.parent.chmod(0o700)
        except OSError:
            pass
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS profile (
            id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, field TEXT NOT NULL,
            value TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'user_explicit',
            confidence TEXT NOT NULL DEFAULT 'medium',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dialogues (
            id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, seq INTEGER NOT NULL,
            dialogued_at TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT '', parent_feeling TEXT NOT NULL DEFAULT '',
            child_observation TEXT NOT NULL DEFAULT '', change_point TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dialogues ON dialogues(subject_id, seq);
        CREATE TABLE IF NOT EXISTS operations (
            op_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, action TEXT NOT NULL,
            before_json TEXT NOT NULL, after_json TEXT NOT NULL
        );
        """
    )
    conn.commit()
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return conn


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def require_enabled(conn) -> None:
    if get_setting(conn, "consent_enabled") != "true":
        raise MemoryErrorWithCode("CONSENT_REQUIRED", "档案尚未获得家长同意")
    if get_setting(conn, "paused") == "true":
        raise MemoryErrorWithCode("MEMORY_PAUSED", "档案当前已暂停")


def row_dict(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def clean_text(name: str, value: Any, maximum: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if required and not value:
        raise MemoryErrorWithCode("INVALID_DELTA", f"{name} 不能为空")
    if len(value) > maximum:
        raise MemoryErrorWithCode("INVALID_DELTA", f"{name} 超过 {maximum} 字符")
    if any(ord(c) < 32 and c not in "\t\n" for c in value):
        raise MemoryErrorWithCode("INVALID_DELTA", f"{name} 含控制字符")
    for banned in BLOCKLIST:  # 值里也不许出现可识别信息
        if banned in value:
            raise MemoryErrorWithCode("VALUE_BLOCKED", f"{name} 含禁止保存的内容（{banned}）")
    return value


def validate_profile(raw: dict) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MemoryErrorWithCode("INVALID_DELTA", "变更必须是 JSON 对象")
    field = clean_text("field", raw.get("field"), 64)
    for banned in BLOCKLIST:
        if banned in field:
            raise MemoryErrorWithCode("FIELD_BLOCKED", f"字段名含禁止内容（{banned}）")
    if field not in PROFILE_FIELDS:
        raise MemoryErrorWithCode(
            "FIELD_NOT_ALLOWED",
            f"档案不接受字段 {field}；只接受：{'、'.join(sorted(PROFILE_FIELDS))}")
    return {
        "subject_id": clean_text("subject_id", raw.get("subject_id") or DEFAULT_SUBJECT, 64),
        "field": field,
        "value": clean_text("value", raw.get("value"), MAX_VALUE_CHARS),
        "source_type": clean_text("source_type", raw.get("source_type", "user_explicit"), 32),
        "confidence": clean_text("confidence", raw.get("confidence", "medium"), 16),
    }


def validate_dialogue(raw: dict) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MemoryErrorWithCode("INVALID_DELTA", "变更必须是 JSON 对象")
    state = clean_text("state", raw.get("state", ""), 16, required=False)
    if state and state not in STATES:
        raise MemoryErrorWithCode("INVALID_STATE", f"沟通状态取值非法：{state}")
    if not state and not clean_text("summary", raw.get("summary", ""), 200, required=False):
        raise MemoryErrorWithCode("INVALID_DELTA", "至少要写一句“这次聊了什么”或一个沟通状态")
    return {
        "subject_id": clean_text("subject_id", raw.get("subject_id") or DEFAULT_SUBJECT, 64),
        "dialogued_at": clean_text("dialogued_at", raw.get("dialogued_at") or now_iso()[:10], 32),
        "summary": clean_text("summary", raw.get("summary", ""), 200, required=False),
        "state": state,
        "parent_feeling": clean_text("parent_feeling", raw.get("parent_feeling", ""), 120, required=False),
        "child_observation": clean_text("child_observation", raw.get("child_observation", ""), 200, required=False),
        "change_point": clean_text("change_point", raw.get("change_point", ""), 200, required=False),
    }


def prune_ops(conn) -> None:
    conn.execute("DELETE FROM operations WHERE op_id NOT IN ("
                 "SELECT op_id FROM operations ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                 (MAX_OPERATIONS,))


def log_op(conn, action: str, before: list, after: list) -> str:
    op_id = uuid.uuid4().hex
    conn.execute("INSERT INTO operations(op_id, created_at, action, before_json, after_json) "
                 "VALUES(?, ?, ?, ?, ?)",
                 (op_id, now_iso(), action, json.dumps(before, ensure_ascii=False),
                  json.dumps(after, ensure_ascii=False)))
    prune_ops(conn)
    return op_id


# ---------------------------------------------------------------- 命令
def cmd_status(_):
    target = db_path()
    if not target.exists():
        emit({"exists": False, "consent_enabled": False, "paused": False,
              "policy_version": POLICY_VERSION, "path": str(target)})
        return
    with connect() as conn:
        emit({
            "exists": True,
            "consent_enabled": get_setting(conn, "consent_enabled") == "true",
            "consent_at": get_setting(conn, "consent_at") or None,
            "paused": get_setting(conn, "paused") == "true",
            "policy_version": get_setting(conn, "policy_version", POLICY_VERSION),
            "profile_count": conn.execute("SELECT COUNT(*) n FROM profile").fetchone()["n"],
            "dialogue_count": conn.execute("SELECT COUNT(*) n FROM dialogues").fetchone()["n"],
            "undo_count": conn.execute("SELECT COUNT(*) n FROM operations").fetchone()["n"],
            "path": str(target),
        })


def cmd_enable(args):
    if not args.confirm:
        raise MemoryErrorWithCode("CONFIRMATION_REQUIRED", "需要 --confirm 表示家长已明确同意")
    with connect(create=True) as conn:
        ts = now_iso()
        if not get_setting(conn, "consent_at"):
            set_setting(conn, "consent_at", ts)
        set_setting(conn, "consent_enabled", "true")
        set_setting(conn, "paused", "false")
        set_setting(conn, "policy_version", POLICY_VERSION)
        conn.commit()
    emit({"ok": True, "consent_enabled": True, "paused": False})


def cmd_pause(_):
    with connect() as conn:
        if get_setting(conn, "consent_enabled") != "true":
            raise MemoryErrorWithCode("CONSENT_REQUIRED", "档案尚未启用")
        set_setting(conn, "paused", "true")
        conn.commit()
    emit({"ok": True, "paused": True})


def cmd_resume(_):
    with connect() as conn:
        if get_setting(conn, "consent_enabled") != "true":
            raise MemoryErrorWithCode("CONSENT_REQUIRED", "档案尚未启用")
        set_setting(conn, "paused", "false")
        conn.commit()
    emit({"ok": True, "paused": False})


def load_raw(args) -> dict:
    if args.file:
        return json.loads(Path(args.file).read_text(encoding="utf-8"))
    if args.json:
        return json.loads(args.json)
    return json.load(sys.stdin)


def cmd_apply(args):
    delta = validate_profile(load_raw(args))
    ts = now_iso()
    with connect() as conn:
        require_enabled(conn)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM profile WHERE subject_id = ? AND field = ? LIMIT 1",
            (delta["subject_id"], delta["field"])).fetchone()
        before = [row_dict(existing)] if existing else []
        mid, created = (existing["id"], existing["created_at"]) if existing else (uuid.uuid4().hex, ts)
        row = {"id": mid, **delta, "created_at": created, "updated_at": ts}
        conn.execute(
            "INSERT INTO profile(id, subject_id, field, value, source_type, confidence, created_at, updated_at) "
            "VALUES(:id,:subject_id,:field,:value,:source_type,:confidence,:created_at,:updated_at) "
            "ON CONFLICT(id) DO UPDATE SET value=excluded.value, source_type=excluded.source_type, "
            "confidence=excluded.confidence, updated_at=excluded.updated_at", row)
        op_id = log_op(conn, "apply", before, [row])
        conn.commit()
    emit({"ok": True, "op_id": op_id, "profile": row})


def cmd_dialogue_add(args):
    delta = validate_dialogue(load_raw(args))
    with connect() as conn:
        require_enabled(conn)
        conn.execute("BEGIN IMMEDIATE")
        nxt = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 n FROM dialogues WHERE subject_id = ?",
            (delta["subject_id"],)).fetchone()["n"]
        row = {"id": uuid.uuid4().hex, **delta, "seq": nxt, "created_at": now_iso()}
        conn.execute(
            "INSERT INTO dialogues(id, subject_id, seq, dialogued_at, summary, state, "
            "parent_feeling, child_observation, change_point, created_at) "
            "VALUES(:id,:subject_id,:seq,:dialogued_at,:summary,:state,"
            ":parent_feeling,:child_observation,:change_point,:created_at)", row)
        # 超出上限时丢掉最早的一条
        total = conn.execute("SELECT COUNT(*) n FROM dialogues").fetchone()["n"]
        if total > MAX_DIALOGUES:
            conn.execute("DELETE FROM dialogues WHERE id IN (SELECT id FROM dialogues "
                         "ORDER BY created_at ASC LIMIT ?)", (total - MAX_DIALOGUES,))
        op_id = log_op(conn, "dialogue-add", [], [row])
        conn.commit()
    emit({"ok": True, "op_id": op_id, "dialogue": row})


def cmd_dialogue_show(args):
    with connect() as conn:
        cond = "WHERE subject_id = ?" if args.subject_id else ""
        params = (args.subject_id,) if args.subject_id else ()
        rows = conn.execute(
            f"SELECT * FROM dialogues {cond} ORDER BY seq DESC LIMIT ?",
            params + (args.limit,)).fetchall()
    rows = list(reversed(rows))  # 按时间正序输出
    emit({"count": len(rows), "dialogues": [row_dict(r) for r in rows]})


def cmd_dialogue_delete(args):
    """删掉一条对话记录。--id 删指定那条；不给则删最近一条。不给 --confirm 不删。"""
    if not args.confirm:
        raise MemoryErrorWithCode("CONFIRMATION_REQUIRED", "需要 --confirm 才会删除")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if args.id:
            row = conn.execute("SELECT * FROM dialogues WHERE id = ?", (args.id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM dialogues ORDER BY created_at DESC, seq DESC LIMIT 1").fetchone()
        if not row:
            raise MemoryErrorWithCode("NOT_FOUND", "没有找到要删除的记录")
        conn.execute("DELETE FROM dialogues WHERE id = ?", (row["id"],))
        op_id = log_op(conn, "dialogue-delete", [row_dict(row)], [])
        conn.commit()
    emit({"ok": True, "op_id": op_id, "deleted": row_dict(row)})


def cmd_undo(args):
    with connect() as conn:
        op = (conn.execute("SELECT * FROM operations WHERE op_id = ?", (args.op_id,)).fetchone()
              if args.op_id else
              conn.execute("SELECT * FROM operations ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone())
        if not op:
            raise MemoryErrorWithCode("NOTHING_TO_UNDO", "没有可撤销的更新")
        before, after = json.loads(op["before_json"]), json.loads(op["after_json"])
        conn.execute("BEGIN IMMEDIATE")
        for r in after:
            conn.execute("DELETE FROM profile WHERE id = ?", (r["id"],))
            conn.execute("DELETE FROM dialogues WHERE id = ?", (r["id"],))
        for r in before:
            cols = ",".join(r.keys())
            ph = ",".join(":" + k for k in r)
            table = "profile" if "field" in r else "dialogues"
            conn.execute(f"INSERT OR REPLACE INTO {table}({cols}) VALUES({ph})", r)
        conn.execute("DELETE FROM operations WHERE op_id = ?", (op["op_id"],))
        conn.commit()
    emit({"ok": True, "undone_op_id": op["op_id"]})


def cmd_show(args):
    with connect() as conn:
        cond = "WHERE subject_id = ?" if args.subject_id else ""
        params = (args.subject_id,) if args.subject_id else ()
        profile = [row_dict(r) for r in conn.execute(
            f"SELECT * FROM profile {cond} ORDER BY updated_at DESC", params)]
        dialogues = [row_dict(r) for r in conn.execute(
            f"SELECT * FROM dialogues {cond} ORDER BY seq", params)]
    emit({"profile": profile, "dialogues": dialogues})


def cmd_context(args):
    with connect() as conn:
        require_enabled(conn)
        sid = args.subject_id
        params = (sid,) if sid else ()
        cond = "WHERE subject_id = ?" if sid else ""
        profile = [row_dict(r) for r in conn.execute(
            f"SELECT * FROM profile {cond} ORDER BY updated_at DESC", params)]
        dialogues = [row_dict(r) for r in conn.execute(
            f"SELECT * FROM dialogues {cond} ORDER BY seq ASC", params)]
    # 最近几条优先保留，超预算就从最早的一条开始丢
    compact = {
        "profile": [{"field": r["field"], "value": r["value"], "updated_at": r["updated_at"]}
                    for r in profile],
        "dialogues": [{"seq": d["seq"], "dialogued_at": d["dialogued_at"], "summary": d["summary"],
                       "state": d["state"], "parent_feeling": d["parent_feeling"],
                       "child_observation": d["child_observation"], "change_point": d["change_point"]}
                      for d in dialogues],
    }
    while compact["dialogues"] and len(json.dumps(compact, ensure_ascii=False)) > args.max_chars:
        compact["dialogues"].pop(0)
    emit({"max_chars": args.max_chars, **compact})


def cmd_forget(args):
    if not args.confirm:
        raise MemoryErrorWithCode("CONFIRMATION_REQUIRED", "需要 --confirm 才能永久删除")
    sid = clean_text("subject_id", args.subject_id, 64)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("SELECT COUNT(*) n FROM profile WHERE subject_id = ?", (sid,)).fetchone()["n"]
        n += conn.execute("SELECT COUNT(*) n FROM dialogues WHERE subject_id = ?", (sid,)).fetchone()["n"]
        conn.execute("DELETE FROM profile WHERE subject_id = ?", (sid,))
        conn.execute("DELETE FROM dialogues WHERE subject_id = ?", (sid,))
        conn.execute("DELETE FROM operations")
        conn.commit()
        conn.execute("VACUUM")
    emit({"ok": True, "subject_id": sid, "deleted": n, "undo_history_cleared": True})


def _unlink_db():
    target = db_path()
    if target.exists():
        target.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(target) + suffix)
        if side.exists():
            side.unlink()


def cmd_revoke(args):
    if not args.confirm:
        raise MemoryErrorWithCode("CONFIRMATION_REQUIRED", "需要 --confirm 才能撤回同意")
    if args.delete:
        _unlink_db()
        emit({"ok": True, "consent_enabled": False, "deleted": True})
        return
    with connect() as conn:
        set_setting(conn, "consent_enabled", "false")
        set_setting(conn, "paused", "true")
        conn.commit()
    emit({"ok": True, "consent_enabled": False, "deleted": False})


def cmd_clear(args):
    if not args.confirm:
        raise MemoryErrorWithCode("CONFIRMATION_REQUIRED", "需要 --confirm 才能永久清空")
    _unlink_db()
    emit({"ok": True, "deleted": True})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    for name, fn in (("pause", cmd_pause), ("resume", cmd_resume)):
        sub.add_parser(name).set_defaults(func=fn)

    en = sub.add_parser("enable"); en.add_argument("--confirm", action="store_true")
    en.set_defaults(func=cmd_enable)

    def with_source(sp):
        grp = sp.add_mutually_exclusive_group()
        grp.add_argument("--json")
        grp.add_argument("--file")
        return sp

    apply_cmd = with_source(sub.add_parser("apply")); apply_cmd.set_defaults(func=cmd_apply)
    da = with_source(sub.add_parser("dialogue-add")); da.set_defaults(func=cmd_dialogue_add)
    dd = sub.add_parser("dialogue-delete")
    dd.add_argument("--id", help="要删的记录 id；不给则删最近一条")
    dd.add_argument("--last", action="store_true", help="删最近一条（默认行为，写明更清楚）")
    dd.add_argument("--confirm", action="store_true")
    dd.set_defaults(func=cmd_dialogue_delete)

    ds = sub.add_parser("dialogue-show")
    ds.add_argument("--subject-id")
    ds.add_argument("--limit", type=int, default=30)
    ds.set_defaults(func=cmd_dialogue_show)

    un = sub.add_parser("undo"); un.add_argument("--op-id"); un.set_defaults(func=cmd_undo)
    sh = sub.add_parser("show"); sh.add_argument("--subject-id"); sh.set_defaults(func=cmd_show)
    cx = sub.add_parser("context")
    cx.add_argument("--subject-id")
    cx.add_argument("--max-chars", type=int, default=4000)
    cx.set_defaults(func=cmd_context)

    fg = sub.add_parser("forget"); fg.add_argument("subject_id")
    fg.add_argument("--confirm", action="store_true"); fg.set_defaults(func=cmd_forget)
    rv = sub.add_parser("revoke"); rv.add_argument("--delete", action="store_true")
    rv.add_argument("--confirm", action="store_true"); rv.set_defaults(func=cmd_revoke)
    cl = sub.add_parser("clear"); cl.add_argument("--confirm", action="store_true")
    cl.set_defaults(func=cmd_clear)
    return p


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except (MemoryErrorWithCode, json.JSONDecodeError, OSError, sqlite3.Error) as exc:
        code = exc.code if isinstance(exc, MemoryErrorWithCode) else exc.__class__.__name__.upper()
        emit({"ok": False, "error": {"code": code, "message": str(exc)}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
