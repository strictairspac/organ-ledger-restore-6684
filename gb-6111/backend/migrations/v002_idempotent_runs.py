from sqlalchemy import text

from src.models import audit, cases, reviews, runs, users  # Register the tables.
from src.models.base import metadata


def upgrade(connection):
    # Version 1 created the workbench tables.
    metadata.create_all(connection)
    # Version 2 makes a rerun of the same case + parameter idempotent. The
    # partial-style unique index is created conditionally so existing stacks
    # can upgrade without dropping their history.
    existing = connection.execute(text(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'uq_runs_case_parameter'"
    )).first()
    if existing is None:
        duplicates = connection.execute(text(
            "SELECT case_id, parameter FROM runs "
            "GROUP BY case_id, parameter HAVING count(*) > 1 LIMIT 1"
        )).first()
        if duplicates is not None:
            raise RuntimeError(
                "存在同一资料与参数的重复批次，请先清理历史批次再启用重跑幂等约束"
            )
        connection.execute(text(
            "CREATE UNIQUE INDEX uq_runs_case_parameter "
            "ON runs (case_id, parameter)"
        ))
