from time import time

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.constants.job_state import JobState
from src.domain.scenario import CONFIG
from src.models.cases import cases
from src.models.runs import runs
from . import audit, cases as case_service


def query():
    return select(runs, cases.c.title.label("case_title")).join(cases, cases.c.id == runs.c.case_id)


def get(db, run_id):
    run = db.execute(query().where(runs.c.id == run_id)).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    return dict(run)


def list_entries(db):
    return [dict(row) for row in db.execute(query().order_by(runs.c.id.desc()).limit(100)).mappings()]


def enqueue(db, case_id, parameter, actor):
    """同一资料与同一参数的重跑复用既有批次，绝不新增重复结果。"""
    if parameter > CONFIG["max_parameter"]:
        raise HTTPException(422, f"参数不能超过 {CONFIG['max_parameter']}")
    if parameter < 1:
        raise HTTPException(422, "参数至少为 1")
    case_service.get(db, case_id)
    statement = (
        pg_insert(runs)
        .values(
            case_id=case_id, status=JobState.QUEUED, parameter=parameter,
            actor=actor, created_at=int(time()),
        )
        .on_conflict_do_nothing(index_elements=["case_id", "parameter"])
        .returning(runs.c.id)
    )
    run_id = db.execute(statement).scalar_one_or_none()
    if run_id is None:
        # 已有同资料同参数批次：直接返回既有批次，保持结果唯一。
        existing = db.execute(
            query().where(runs.c.case_id == case_id, runs.c.parameter == parameter)
        ).mappings().one()
        return dict(existing)
    audit.record(db, "enqueue", actor, "run", run_id)
    return get(db, run_id)
