"""QC 子系统的 SQLite 持久层。

状态机只认这里的表，不另存内存状态：进程重启后批次是否被卡住、哪份化验单被
放行引用，全部由落盘数据决定。``audit_event`` 追加写入，成功和被门控拒绝的
动作都留痕。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT 'kg',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_specs (
    id          TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    name        TEXT NOT NULL,
    method      TEXT NOT NULL DEFAULT '',
    min_value   REAL,
    max_value   REAL,
    unit        TEXT NOT NULL DEFAULT '',
    seq         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sampling_plans (
    id          TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    point_code  TEXT NOT NULL,
    point_name  TEXT NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    frequency   TEXT NOT NULL DEFAULT '每批',
    seq         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (material_id, point_code)
);

CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    batch_no        TEXT NOT NULL UNIQUE,
    material_id     TEXT NOT NULL REFERENCES materials(id),
    supplier        TEXT NOT NULL DEFAULT '',
    quantity        REAL,
    received_at     TEXT NOT NULL,
    location        TEXT NOT NULL DEFAULT '待检区',
    status          TEXT NOT NULL DEFAULT 'pending_sampling',
    rejected_flag   INTEGER NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    id              TEXT PRIMARY KEY,
    sample_no       TEXT NOT NULL UNIQUE,
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    point_code      TEXT NOT NULL,
    point_name      TEXT NOT NULL,
    seq             INTEGER NOT NULL DEFAULT 0,
    origin_sample_id TEXT REFERENCES samples(id),
    required_flag   INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'planned',
    planned_at      TEXT NOT NULL,
    taken_at        TEXT,
    taken_by        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reports (
    id                  TEXT PRIMARY KEY,
    report_no           TEXT NOT NULL UNIQUE,
    sample_id           TEXT NOT NULL REFERENCES samples(id),
    batch_id            TEXT NOT NULL REFERENCES batches(id),
    kind                TEXT NOT NULL DEFAULT 'initial',
    source_retest_id    TEXT,
    overall_verdict     TEXT NOT NULL DEFAULT 'pending',
    status              TEXT NOT NULL DEFAULT 'pending',
    analyst             TEXT NOT NULL DEFAULT '',
    analyzed_at         TEXT,
    submitted_at        TEXT,
    submitted_by        TEXT NOT NULL DEFAULT '',
    reviewed_at         TEXT,
    reviewed_by         TEXT NOT NULL DEFAULT '',
    review_note         TEXT NOT NULL DEFAULT '',
    superseded_by       TEXT REFERENCES reports(id),
    note                TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS report_results (
    id          TEXT PRIMARY KEY,
    report_id   TEXT NOT NULL REFERENCES reports(id),
    spec_name   TEXT NOT NULL,
    method      TEXT NOT NULL DEFAULT '',
    value       REAL,
    min_value   REAL,
    max_value   REAL,
    unit        TEXT NOT NULL DEFAULT '',
    verdict     TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS attachments (
    id          TEXT PRIMARY KEY,
    report_id   TEXT NOT NULL REFERENCES reports(id),
    filename    TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes  INTEGER NOT NULL,
    stored_path TEXT NOT NULL,
    uploaded_by TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retests (
    id              TEXT PRIMARY KEY,
    retest_no       TEXT NOT NULL UNIQUE,
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    sample_id       TEXT NOT NULL REFERENCES samples(id),
    reason          TEXT NOT NULL,
    requested_by    TEXT NOT NULL DEFAULT '',
    requested_at    TEXT NOT NULL,
    new_sample_id   TEXT REFERENCES samples(id),
    status          TEXT NOT NULL DEFAULT 'open',
    closed_at       TEXT,
    note            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS concessions (
    id            TEXT PRIMARY KEY,
    concession_no TEXT NOT NULL UNIQUE,
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    reason        TEXT NOT NULL,
    disposition   TEXT NOT NULL DEFAULT '让步接收',
    applied_by    TEXT NOT NULL DEFAULT '',
    applied_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    decided_by    TEXT NOT NULL DEFAULT '',
    decided_at    TEXT,
    decision_note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS releases (
    id            TEXT PRIMARY KEY,
    release_no    TEXT NOT NULL UNIQUE,
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    basis         TEXT NOT NULL,
    destination   TEXT NOT NULL DEFAULT '产线',
    released_by   TEXT NOT NULL DEFAULT '',
    released_at   TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    concession_id TEXT REFERENCES concessions(id)
);

CREATE TABLE IF NOT EXISTS release_reports (
    release_id  TEXT NOT NULL REFERENCES releases(id),
    report_id   TEXT NOT NULL REFERENCES reports(id),
    PRIMARY KEY (release_id, report_id)
);

CREATE TABLE IF NOT EXISTS doc_sequences (
    prefix TEXT NOT NULL,
    day    TEXT NOT NULL,
    seq    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (prefix, day)
);

CREATE TABLE IF NOT EXISTS audit_events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL DEFAULT '',
    batch_id  TEXT NOT NULL DEFAULT '',
    outcome   TEXT NOT NULL,
    reason    TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_samples_batch ON samples(batch_id);
CREATE INDEX IF NOT EXISTS idx_reports_batch ON reports(batch_id);
CREATE INDEX IF NOT EXISTS idx_reports_sample ON reports(sample_id);
CREATE INDEX IF NOT EXISTS idx_retests_batch ON retests(batch_id);
CREATE INDEX IF NOT EXISTS idx_concessions_batch ON concessions(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_events(at);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_events(batch_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class QcStore:
    """带连接锁的 SQLite 封装。

    HTTP 服务是多线程的，因此一把锁串行化写事务；每个动作内部用
    ``transaction()`` 保证「状态跃迁 + 审计」原子提交，门控拒绝时整体回滚，
    不会留下半个批次。
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._init_schema()

    # ----------------------------------------------------------- 初始化
    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._migrate()
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"QC 数据库结构版本不匹配：期望 {SCHEMA_VERSION}，实际 {row['value']}"
                )

    def _migrate(self) -> None:
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(audit_events)").fetchall()}
        if "batch_id" not in cols:
            self._conn.execute("ALTER TABLE audit_events ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_events(batch_id)")

    # ----------------------------------------------------------- 基础工具
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        return self._conn.executemany(sql, seq)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    def audit(
        self,
        *,
        actor: str,
        action: str,
        target: str = "",
        batch_id: str = "",
        outcome: str,
        reason: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO audit_events(at, actor, action, target, batch_id, outcome, reason, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utc_now(),
                actor,
                action,
                target,
                batch_id,
                outcome,
                reason,
                json.dumps(dict(detail or {}), ensure_ascii=False),
            ),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(row) for row in rows]  # type: ignore[misc]


__all__ = ["QcStore", "SCHEMA_VERSION", "utc_now", "row_to_dict", "rows_to_dicts"]
