"""维修卷复原管道验收：重跑幂等、整批阻断、选中闭包与摘要一致性。"""
import pytest
from fastapi import HTTPException
from sqlalchemy import func, insert, select, text

from src.constants.job_state import JobState
from src.domain import scenario
from src.domain.fixtures import FIXTURES
from src.domain.scenario import OUTCOME_BLOCKED, OUTCOME_RESTORED
from src.models.cases import cases
from src.models.runs import runs
from src.services import runs as runs_service
from src.utils.serial import content_digest

GOOD, CORRUPT = FIXTURES[0]["payload"], FIXTURES[1]["payload"]


# ---------- 要求一：同一资料与参数重跑不新增重复结果 ----------

def test_rerun_is_deterministic(db):
    first = scenario.run(GOOD, 20, db)
    again = scenario.run(GOOD, 20, db)
    assert content_digest(again) == content_digest(first)
    assert [r["证明摘要"] for r in again["rows"]] == [r["证明摘要"] for r in first["rows"]]
    assert [r["还原行数"] for r in again["rows"]] == [r["还原行数"] for r in first["rows"]]


def test_enqueue_dedupes_same_case_and_parameter(db):
    case_id = db.execute(insert(cases).values(
        code="CASE-DEDUPE", title="去重资料", description="d", payload=GOOD, created_by="operator",
    )).inserted_primary_key[0]
    first = runs_service.enqueue(db, case_id, 20, "operator")
    second = runs_service.enqueue(db, case_id, 20, "operator")
    other = runs_service.enqueue(db, case_id, 1, "operator")
    assert first["id"] == second["id"]
    assert other["id"] != first["id"]
    total = db.execute(
        select(func.count()).select_from(runs).where(runs.c.case_id == case_id)
    ).scalar_one()
    assert total == 2


# ---------- 要求二：全部核验通过后一次性切换；任一段损坏整批阻断并留证 ----------

def test_damage_blocks_whole_batch_and_keeps_evidence(db):
    result = scenario.run(CORRUPT, 20, db)
    assert result["outcome"] == OUTCOME_BLOCKED
    assert result["metrics"] == {"入选工单": 1, "还原工单": 0, "损伤段": 1}
    damaged = [r for r in result["rows"] if r["核验"] == "段受损"]
    assert len(damaged) == 1 and damaged[0]["归档段"] == "测记"
    # 受损段同时保留证明摘要与传输后实测摘要，二者不同即损伤证据。
    assert damaged[0]["证明摘要"] != damaged[0]["重新导出摘要"]
    assert sum(r["核验"] == "待导入" for r in result["rows"]) == 2
    assert all(r["还原行数"] == 0 for r in result["rows"])


def test_blocked_run_leaves_no_target_tables(db_engine):
    # 用同一条连接执行阻断场景并提交：提交后 ON COMMIT DROP 应清除全部临时表。
    conn = db_engine.connect()
    trans = conn.begin()
    try:
        scenario.run(CORRUPT, 20, conn)
        trans.commit()
    finally:
        conn.close()
    with db_engine.begin() as check:
        left = check.execute(
            text("SELECT count(*) FROM pg_class WHERE relname LIKE 'organ_target_%'")
        ).scalar_one()
    assert left == 0


def test_each_relation_is_detectable_when_damaged(db):
    for relation in ("organs", "visits", "readings"):
        payload = {**GOOD, "simulate_damage": relation}
        result = scenario.run(payload, 20, db)
        assert result["outcome"] == OUTCOME_BLOCKED
        assert result["metrics"]["损伤段"] == 1
        assert result["summary"].count("阻断") >= 1


# ---------- 要求三：成功结果只含选中闭包，且摘要/行数/重导 SHA 一致 ----------

def test_success_contains_only_selected_closure(db):
    result = scenario.run(GOOD, 20, db)
    assert result["outcome"] == OUTCOME_RESTORED
    by_label = {row["归档段"]: row for row in result["rows"]}
    assert by_label["琴体"]["还原行数"] == 2          # W-1131→ORG-17、W-1138→ORG-22
    assert by_label["维修工单"]["还原行数"] == 2      # 仅 W-1131、W-1138
    assert by_label["测记"]["还原行数"] == 3          # D-2、D-3、D-4
    # 不再向结果暴露源卷总行数，避免未选资料混入。
    assert all("源卷行数" not in row for row in result["rows"])


def test_parameter_limits_selected_visits(db):
    result = scenario.run(GOOD, 1, db)
    by_label = {row["归档段"]: row for row in result["rows"]}
    assert result["metrics"]["入选工单"] == 1
    assert by_label["维修工单"]["还原行数"] == 1
    assert by_label["琴体"]["还原行数"] == 1
    assert by_label["测记"]["还原行数"] == 2          # W-1131 下的 D-2、D-3


def test_digests_and_row_counts_stay_consistent(db):
    result = scenario.run(GOOD, 20, db)
    for row in result["rows"]:
        assert row["核验"] == "确认"
        assert row["证明摘要"] == row["重新导出摘要"]
        assert len(row["证明摘要"]) == 64
        assert row["还原行数"] == row["选中行数"]


def test_export_refuses_blocked_run(db_engine):
    from src.routes.runs import export as export_route
    from src.services import execution

    with db_engine.begin() as conn:
        case_id = conn.execute(insert(cases).values(
            code="CASE-EXPORT", title="导出守卫", description="d",
            payload=CORRUPT, created_by="operator",
        )).inserted_primary_key[0]
        run_id = runs_service.enqueue(conn, case_id, 20, "operator")["id"]
    # 入队事务已提交，worker 用连接池中的独立连接消费，不会与测试连接互锁。
    assert execution.service_one() is True
    with pytest.raises(HTTPException) as exc:
        export_route(run_id)
    assert exc.value.status_code == 409
