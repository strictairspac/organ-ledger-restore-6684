from sqlalchemy import text

from .archive import apply_archive, damage_segment, export_selection, reexport
from .fixtures import FIXTURES
from .relations import RELATIONS, create_tables, validate, write_source

CONFIG = {
    "title": "风栎琴作·管风琴维护卷复原",
    "team": "风栎琴作 12 人巡修与档案组",
    "description": "从整月维护卷择单归档，把关联琴体、维修工单和测记复原到隔离表，再用证据确认缺卷或传输损伤。",
    "direction": "逻辑备份与选择性恢复",
    "mechanism": "PostgreSQL 原生 COPY、外键依赖闭包和逐段 SHA-256 字节核验。",
    "modules": ["维修库卷宗", "复原范围", "归档演练", "复原证明"],
    "unit": "维护卷",
    "max_parameter": 100,
    "default_parameter": 20,
    "parameter_label": "本次最多复原工单数",
    "facts": ["源卷目录", "外键闭包", "隔离复原"],
    "palette": 11,
    "fixture": "organ-ledger-2026-11.copy",
}

OUTCOME_RESTORED = "restored"
OUTCOME_BLOCKED = "blocked"


def expected_rows(payload, selected):
    """入选闭包内每段应有的行数：选中工单、其父琴体及其全部子测记。"""
    selected_set = set(selected)
    organs = {
        visit["organ_id"]
        for visit in payload["visits"]
        if visit["visit_id"] in selected_set
    }
    return {
        "organs": len(organs),
        "visits": len(selected_set),
        "readings": sum(
            1 for reading in payload["readings"] if reading["visit_id"] in selected_set
        ),
    }


def _blocked_report(segments, expected, selected):
    rows = []
    for rel, segment in zip(RELATIONS, segments, strict=True):
        rows.append({
            "归档段": rel.label,
            "选中行数": expected[rel.key],
            "归档字节": len(segment.data),
            "证明摘要": segment.digest,
            "重新导出摘要": segment.actual_digest(),
            "还原行数": 0,
            "核验": "段受损" if not segment.verified() else "待导入",
        })
    damaged = [rel.label for rel, segment in zip(RELATIONS, segments) if not segment.verified()]
    return {
        "outcome": OUTCOME_BLOCKED,
        "summary": f"归档校验失败（{len(damaged)} 段：{'、'.join(damaged)}），整批阻断，目标表未创建、未写入任何数据。",
        "metrics": {
            "入选工单": len(selected),
            "还原工单": 0,
            "损伤段": sum(not segment.verified() for segment in segments),
        },
        "rows": rows,
    }


def run(payload, parameter, db):
    validate(payload)
    source = create_tables(db, "source")
    write_source(db, source, payload)
    selected = payload["requested"][:parameter]
    expected = expected_rows(payload, selected)

    # 阶段一：导出全部归档段，并在触碰任何目标表之前完成逐段核验。
    segments = damage_segment(export_selection(db, source, selected), payload.get("simulate_damage"))
    if any(not segment.verified() for segment in segments):
        # 任一段损坏即整批阻断；保留每段证明摘要与实测摘要作为受损证据。
        return _blocked_report(segments, expected, selected)

    # 阶段二：全部段核验通过后，才一次性创建目标表并按外键顺序切换导入。
    target = create_tables(db, "target")
    apply_archive(db, target, segments)

    rows = []
    restored_visits = 0
    for rel, segment in zip(RELATIONS, segments, strict=True):
        count = db.execute(text(f"SELECT count(*) FROM {target[rel.key]}")).scalar_one()
        reexported = reexport(db, target, rel)
        if rel.key == "visits":
            restored_visits = count
        if count != expected[rel.key]:
            raise ValueError(
                f"{rel.label}还原行数 {count} 与选中闭包 {expected[rel.key]} 不一致"
            )
        if segment.record_count() != count:
            raise ValueError(f"{rel.label}归档段记录数与目标表行数不一致")
        if reexported.digest != segment.digest:
            raise ValueError(f"{rel.label}重新导出的 SHA-256 与归档证明摘要不一致")
        rows.append({
            "归档段": rel.label,
            "选中行数": expected[rel.key],
            "归档字节": len(segment.data),
            "证明摘要": segment.digest,
            "重新导出摘要": reexported.digest,
            "还原行数": count,
            "核验": "确认",
        })

    return {
        "outcome": OUTCOME_RESTORED,
        "summary": f"已还原 {restored_visits} 张工单；目标外键、还原行数和重新导出的字节摘要均已核验。",
        "metrics": {"入选工单": len(selected), "还原工单": restored_visits, "损伤段": 0},
        "rows": rows,
    }
