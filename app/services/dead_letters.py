"""死信回炉服务（甲案）。

落地形态：补采任务连续失败到上限后，当场把该任务的现场冻成一份**不可变快照**，
同时钉死回炉键（``pin_key``）并附指纹（``fingerprint``）。读侧只认这一份快照，
不回头拼活表；触发器和指纹双保险，任何回刷或篡改都拒读、拒回炉。

回炉动作本身是「闸」：校验指纹 → 在同一个 ``BEGIN IMMEDIATE`` 事务里新建补采
任务并把死信置为「已回炉」、写回炉落点行号；事务写定失败要讲明白，且不留半截落点。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import Config
from app.db import get_db
from app.services import audit as audit_svc
from app.services.errors import ServiceError

DEAD_LETTER_STATUS = ("封存", "已回炉")


def fingerprint_of(snapshot: dict | str) -> str:
    """对快照做规范化 JSON 后的 SHA-256 指纹。"""
    if isinstance(snapshot, str):
        obj = json.loads(snapshot)
    else:
        obj = snapshot
    canonical = json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_dict(snapshot: dict | str) -> dict:
    return json.loads(snapshot) if isinstance(snapshot, str) else dict(snapshot)


def _decorate(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    stored = item.get("snapshot", "")
    try:
        parsed = _snapshot_dict(stored)
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    item["snapshot"] = parsed
    item["fingerprint_ok"] = (
        bool(parsed) and fingerprint_of(parsed) == item.get("fingerprint")
    )
    return item


def seal_from_job(
    conn: sqlite3.Connection, job_id: int, reason: str, now: str | None = None
) -> dict | None:
    """在调用方的事务内把一条补采任务封存为死信。

    幂等：同一 ``job_id`` 已有死信时直接返回既有记录，不重复封存。
    """
    existing = conn.execute(
        "SELECT * FROM backfill_dead_letters WHERE job_id=?", (job_id,)
    ).fetchone()
    if existing:
        return _decorate(existing)

    job = conn.execute("SELECT * FROM backfill_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return None
    row = conn.execute(
        """
        SELECT r.*, b.batch_code, b.site_id, s.name AS site_name
        FROM visit_rows r
        JOIN visit_batches b ON b.id=r.batch_id
        JOIN trial_sites s ON s.id=b.site_id
        WHERE r.id=?
        """,
        (job["row_id"],),
    ).fetchone()
    if not row:
        return None

    failed_at = now or datetime.now().isoformat(timespec="seconds")
    snapshot = {
        "job_id": job["id"],
        "job_code": job["job_code"],
        "row_id": row["id"],
        "subject_key": row["subject_key"],
        "visit_code": row["visit_code"],
        "quality": row["quality"],
        "row_status": row["status"],
        "batch_code": row["batch_code"],
        "site_id": row["site_id"],
        "site_name": row["site_name"],
        "attempt": job["attempt"],
        "last_message": job["message"],
        "reason": reason,
        "failed_at": failed_at,
    }
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    pin_key = f"DL-{job['job_code']}"
    conn.execute(
        """
        INSERT INTO backfill_dead_letters(
          job_id, row_id, pin_key, snapshot, fingerprint, status, sealed_at
        ) VALUES (?, ?, ?, ?, ?, '封存', ?)
        """,
        (job["id"], row["id"], pin_key, canonical, fingerprint_of(canonical), failed_at),
    )
    sealed = conn.execute(
        "SELECT * FROM backfill_dead_letters WHERE job_id=?", (job["id"],)
    ).fetchone()
    return _decorate(sealed)


def sweep_exhausted(db_path: str | None = None) -> int:
    """对账时兜底：把已用完尝试次数却漏封的失败任务补封成死信。

    正常路径由 ``run_job`` 当场封存；这里只兜历史数据与调低上限后的存量，
    避免「失败任务静默消失、缺口永久悬挂」。
    """
    from app.services import settings as settings_svc

    max_attempt = settings_svc.get_int("backfill_max_attempt", 3, db_path=db_path)
    sealed = 0
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT j.id FROM backfill_jobs j
            WHERE j.status='失败' AND j.attempt >= ?
              AND NOT EXISTS (
                SELECT 1 FROM backfill_dead_letters d WHERE d.job_id=j.id
              )
            """,
            (max_attempt,),
        ).fetchall()
        for r in rows:
            if seal_from_job(conn, r["id"], f"对账兜底封存（上限 {max_attempt}）"):
                sealed += 1
    return sealed


def list_dead_letters(
    db_path: str | None = None, status: str | None = None
) -> list[dict]:
    clauses = []
    params: list = []
    if status:
        if status not in DEAD_LETTER_STATUS:
            raise ServiceError("死信状态无效")
        clauses.append("status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM backfill_dead_letters {where} ORDER BY id DESC LIMIT 500",
            params,
        ).fetchall()
        return [_decorate(r) for r in rows]


def get_dead_letter(dead_letter_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM backfill_dead_letters WHERE id=?", (dead_letter_id,)
        ).fetchone()
    if not row:
        raise ServiceError("死信不存在", 404)
    item = _decorate(row)
    if not item["fingerprint_ok"]:
        raise ServiceError("死信快照指纹校验不通过，拒绝读取", 409)
    return item


def next_job_code(conn: sqlite3.Connection) -> str:
    """接续现有补采任务号，避免回炉落点与存量任务撞号。"""
    max_n = 0
    for r in conn.execute("SELECT job_code FROM backfill_jobs").fetchall():
        code = r["job_code"] or ""
        if code.startswith("BF-"):
            try:
                max_n = max(max_n, int(code[3:]))
            except ValueError:
                continue
    return f"BF-{max_n + 1:04d}"


def rework_dead_letter(dead_letter_id: int, db_path: str | None = None) -> dict:
    """死信回炉：校验印记并在同一事务内落一条新的补采任务。"""
    path = Path(db_path) if db_path else Config.DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM backfill_dead_letters WHERE id=?", (dead_letter_id,)
        ).fetchone()
        if not row:
            raise ServiceError("死信不存在", 404)

        item = _decorate(row)
        if not item["fingerprint_ok"]:
            raise ServiceError("死信快照指纹校验不通过，拒绝回炉", 409)
        if row["status"] != "封存":
            raise ServiceError("该死信已回炉，不能重复回炉", 409)
        if row["rework_job_id"] is not None:
            raise ServiceError("该死信已有回炉落点，拒绝重复写入", 409)

        target = conn.execute(
            "SELECT r.*, b.status AS batch_status FROM visit_rows r "
            "JOIN visit_batches b ON b.id=r.batch_id WHERE r.id=?",
            (row["row_id"],),
        ).fetchone()
        if not target:
            raise ServiceError("死信对应访视行已丢失，无法回炉", 409)
        if target["batch_status"] == "已关闭":
            raise ServiceError("已关闭批次不能回炉", 409)

        now = datetime.now().isoformat(timespec="seconds")
        code = next_job_code(conn)
        cur = conn.execute(
            """
            INSERT INTO backfill_jobs(job_code, row_id, status, attempt, message)
            VALUES (?, ?, '排队', 1, '死信回炉重排')
            """,
            (code, row["row_id"]),
        )
        new_job_id = cur.lastrowid
        if target["status"] in ("待检", "已入库"):
            conn.execute(
                "UPDATE visit_rows SET status='待补采' WHERE id=?", (row["row_id"],)
            )
        conn.execute(
            """
            UPDATE backfill_dead_letters
            SET status='已回炉', rework_job_id=?, reworked_at=?
            WHERE id=? AND status='封存'
            """,
            (new_job_id, now, dead_letter_id),
        )
        conn.execute("COMMIT")
    except ServiceError:
        conn.execute("ROLLBACK")
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        raise ServiceError(f"死信回炉事务写入失败：{exc}", 409) from exc
    finally:
        conn.close()

    audit_svc.write_event(
        "死信回炉",
        "backfill_dead_letter",
        dead_letter_id,
        f"落点任务#{new_job_id} 行#{row['row_id']}",
        db_path=db_path,
    )
    from app.services import backfill as backfill_svc

    return {
        "dead_letter": get_dead_letter(dead_letter_id, db_path),
        "job": backfill_svc.get_job(new_job_id, db_path),
    }


def _counts(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT id, snapshot, fingerprint, status, rework_job_id "
            "FROM backfill_dead_letters"
        ).fetchall()
        landed = conn.execute(
            """
            SELECT COUNT(*) AS c FROM backfill_dead_letters d
            JOIN backfill_jobs j ON j.id=d.rework_job_id
            """
        ).fetchone()["c"]
    total = len(rows)
    # 「回炉徽标」走读侧：只数指纹校验通过的那份快照；
    # 「死信条数」走库侧：原始行数。两者拧花即说明快照被改过，须挡掉。
    badge = 0
    for r in rows:
        try:
            parsed = _snapshot_dict(r["snapshot"])
        except (json.JSONDecodeError, TypeError):
            continue
        if parsed and fingerprint_of(parsed) == r["fingerprint"]:
            badge += 1
    pending = sum(1 for r in rows if r["status"] == "封存")
    return {"total": total, "badge": badge, "pending": pending, "landed": landed}


def guard_state(db_path: str | None = None) -> dict:
    """回炉徽标对照死信条数；独亮无落点即驳回，拧花也驳回。"""
    counts = _counts(db_path)
    dead_letter_count = counts["total"]
    rework_badge = counts["badge"]
    landing_rows = counts["landed"]
    consistent = rework_badge == dead_letter_count
    silent_blocked = dead_letter_count > 0 and landing_rows == 0
    return {
        "dead_letter_count": dead_letter_count,
        "rework_badge": rework_badge,
        "pending_rework": counts["pending"],
        "landing_rows": landing_rows,
        "consistent": consistent,
        "silent_blocked": silent_blocked,
        "rejected": silent_blocked or not consistent,
    }


def summary(db_path: str | None = None) -> dict:
    counts = _counts(db_path)
    counts.update(guard_state(db_path))
    return counts
