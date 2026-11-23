"""死信回炉（甲案：不可变快照 + 钉死键 + 指纹）验收用例。

写法约定：先跑这些用例，确认红灯，再补服务与页面。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import get_db
from app.services import backfill, dead_letters, reconcile, settings, visits
from app.services.errors import ServiceError


def _pick_gap_row(db_path: str) -> dict:
    used = {j["row_id"] for j in backfill.list_jobs(db_path=db_path)}
    for row in visits.list_rows(db_path=db_path):
        if (
            row["quality"] != "合格"
            and row["status"] in ("待检", "待补采")
            and row["id"] not in used
        ):
            return row
    raise AssertionError("没有可用于补采的缺口行")


def _drive_to_dead_letter(db_path: str) -> tuple[dict, dict]:
    """把一条补采任务连续跑到失败上限，触发死信封存。"""
    row = _pick_gap_row(db_path)
    settings.update_setting("backfill_max_attempt", "2", db_path=db_path)
    job = backfill.enqueue_from_row(row["id"], db_path=db_path)
    job = backfill.run_job(job["id"], succeed=False, db_path=db_path)
    assert job["status"] == "失败"
    items = dead_letters.list_dead_letters(db_path=db_path)
    assert len(items) == 1, "连续失败达到上限后必须封存死信，不能静默消失"
    return job, items[0]


def test_dead_letter_lands_in_business_table(db_path):
    """先核对死信回炉落点是否入表。"""
    job, letter = _drive_to_dead_letter(db_path)
    with get_db(db_path) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='backfill_dead_letters'"
        ).fetchone()
        archived = conn.execute(
            "SELECT * FROM backfill_dead_letters WHERE job_id=?", (job["id"],)
        ).fetchone()
    assert table is not None
    assert archived is not None
    assert letter["snapshot"]["job_code"] == job["job_code"]
    assert letter["snapshot"]["subject_key"]
    assert letter["pin_key"].startswith(f"DL-{job['job_code']}")
    assert letter["fingerprint"] == dead_letters.fingerprint_of(letter["snapshot"])
    assert letter["fingerprint_ok"] is True
    assert letter["status"] == "封存"


def test_sealed_snapshot_is_read_only_and_rejects_reflush(db_path):
    """死信回炉落点随后再核封存档：不可变快照，拒绝回刷。"""
    _job, letter = _drive_to_dead_letter(db_path)
    before = dead_letters.get_dead_letter(letter["id"], db_path=db_path)
    with pytest.raises(sqlite3.IntegrityError):
        with get_db(db_path) as conn:
            conn.execute(
                "UPDATE backfill_dead_letters SET snapshot=? WHERE id=?",
                ('{"tampered":true}', letter["id"]),
            )
    after = dead_letters.get_dead_letter(letter["id"], db_path=db_path)
    assert after["snapshot"] == before["snapshot"]
    assert after["fingerprint"] == before["fingerprint"]


def test_fingerprint_mismatch_blocks_read_and_rework(db_path):
    """校验印记对不齐却仍放行即挡掉。"""
    _job, letter = _drive_to_dead_letter(db_path)
    with sqlite3.connect(db_path) as raw:
        raw.execute("DROP TRIGGER trg_dead_letter_frozen")
        raw.execute(
            "UPDATE backfill_dead_letters SET snapshot=? WHERE id=?",
            ('{"tampered":true}', letter["id"]),
        )
        raw.commit()

    with pytest.raises(ServiceError) as read_err:
        dead_letters.get_dead_letter(letter["id"], db_path=db_path)
    assert "指纹" in read_err.value.message

    with pytest.raises(ServiceError) as rework_err:
        dead_letters.rework_dead_letter(letter["id"], db_path=db_path)
    assert "指纹" in rework_err.value.message

    with get_db(db_path) as conn:
        got = conn.execute(
            "SELECT status, rework_job_id FROM backfill_dead_letters WHERE id=?",
            (letter["id"],),
        ).fetchone()
    assert got["status"] == "封存"
    assert got["rework_job_id"] is None


def test_rework_creates_landing_job_and_cannot_repeat(db_path):
    """死信回炉提交随即写完落库，回炉落点行必须入表。"""
    _job, letter = _drive_to_dead_letter(db_path)
    result = dead_letters.rework_dead_letter(letter["id"], db_path=db_path)
    assert result["dead_letter"]["status"] == "已回炉"
    assert result["dead_letter"]["rework_job_id"] == result["job"]["id"]
    assert result["job"]["status"] == "排队"
    assert result["job"]["row_id"] == letter["row_id"]
    with get_db(db_path) as conn:
        landed = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_dead_letters WHERE rework_job_id IS NOT NULL"
        ).fetchone()["c"]
    assert landed == 1
    with pytest.raises(ServiceError):
        dead_letters.rework_dead_letter(letter["id"], db_path=db_path)


def test_rework_contention_is_serialized(db_path):
    """两名值班抢写死信回炉，缺串行即会把钉点彼此抹掉。"""
    _job, letter = _drive_to_dead_letter(db_path)

    def _attempt(_i: int):
        try:
            job = dead_letters.rework_dead_letter(letter["id"], db_path=db_path)["job"]
            return ("ok", job["id"], "")
        except ServiceError as exc:  # 另一个值班应拿到明确的争用冲突
            return ("conflict", 0, exc.message)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_attempt, range(2)))

    assert sum(1 for r in results if r[0] == "ok") == 1
    assert sum(1 for r in results if r[0] == "conflict") == 1

    with get_db(db_path) as conn:
        pins = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_dead_letters WHERE rework_job_id IS NOT NULL"
        ).fetchone()["c"]
        landings = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_jobs WHERE message='死信回炉重排'"
        ).fetchone()["c"]
    assert pins == 1
    assert landings == 1


def test_rework_transaction_failure_is_explicit_and_rolls_back(db_path, monkeypatch):
    """死信回炉闸事务写定失败要讲明白，且不留半截落点。"""
    job, letter = _drive_to_dead_letter(db_path)
    monkeypatch.setattr(dead_letters, "next_job_code", lambda conn: job["job_code"])
    with pytest.raises(ServiceError) as err:
        dead_letters.rework_dead_letter(letter["id"], db_path=db_path)
    assert "事务" in err.value.message
    assert "写入失败" in err.value.message
    got = dead_letters.get_dead_letter(letter["id"], db_path=db_path)
    assert got["status"] == "封存"
    assert got["rework_job_id"] is None


def test_badge_matches_count_and_blocks_dead_letter_silence(db_path):
    """回炉徽标对照死信条数；徽标独亮无落点行仍算驳回。"""
    _job, letter = _drive_to_dead_letter(db_path)
    state = dead_letters.guard_state(db_path)
    assert state["dead_letter_count"] == 1
    assert state["rework_badge"] == state["dead_letter_count"]
    assert state["consistent"] is True
    assert state["landing_rows"] == 0
    assert state["silent_blocked"] is True
    assert state["rejected"] is True

    dead_letters.rework_dead_letter(letter["id"], db_path=db_path)
    state = dead_letters.guard_state(db_path)
    assert state["landing_rows"] == 1
    assert state["silent_blocked"] is False
    assert state["rejected"] is False


def test_tampered_snapshot_diverges_badge_and_is_rejected(db_path):
    """回炉徽标对照死信条数拧花即挡死信静默。"""
    _job, letter = _drive_to_dead_letter(db_path)
    with sqlite3.connect(db_path) as raw:
        raw.execute("DROP TRIGGER trg_dead_letter_frozen")
        raw.execute(
            "UPDATE backfill_dead_letters SET snapshot=? WHERE id=?",
            ('{"tampered":true}', letter["id"]),
        )
        raw.commit()

    state = dead_letters.guard_state(db_path)
    assert state["dead_letter_count"] == 1
    assert state["rework_badge"] == 0
    assert state["consistent"] is False
    assert state["rejected"] is True


def test_dashboard_and_matrix_carry_dead_letter_summary(db_path):
    """邻页（访视对账）亮死信条数，矩阵带死信摘要。"""
    _job, _letter = _drive_to_dead_letter(db_path)
    dash = reconcile.dashboard(db_path)
    assert dash["dead_letter_count"] == 1
    assert dash["dead_letter_rework_badge"] == 1
    assert dash["dead_letter_guard"]["rejected"] is True

    matrix = reconcile.site_gap_matrix(db_path)
    assert any(int(row["dead_letters"]) >= 1 for row in matrix)


def test_settings_expose_rework_caliber(db_path):
    """台参数只展死信回炉口径。"""
    item = settings.get_setting("dead_letter_rework_caliber", db_path)
    assert item["label"] == "死信回炉口径"
    assert item["value"]


def test_reconcile_sweeps_legacy_exhausted_job_into_dead_letter(db_path):
    """存量漏封的失败任务不得静默消失：对账兜底补封。"""
    settings.update_setting("backfill_max_attempt", "1", db_path=db_path)
    row = _pick_gap_row(db_path)
    with get_db(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO backfill_jobs(job_code, row_id, status, attempt, message) "
            "VALUES ('BF-LEGACY', ?, '失败', 5, '历史漏封')",
            (row["id"],),
        )
        legacy_job_id = cur.lastrowid
    assert dead_letters.list_dead_letters(db_path=db_path) == []

    reconcile.run_reconcile(db_path)

    items = dead_letters.list_dead_letters(db_path=db_path)
    assert legacy_job_id in [i["job_id"] for i in items]
    assert all(i["status"] == "封存" for i in items)
