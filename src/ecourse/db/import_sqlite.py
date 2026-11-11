from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, delete, func, insert, select
from sqlalchemy.exc import SQLAlchemyError

from . import Database
from .models import Base, WorkerHeartbeat


def import_sqlite(settings, source_path: Path, *, chunk_size: int = 500) -> dict[str, int]:
    """Copy a current eCourse SQLite database into an empty configured database."""
    source_path = Path(source_path).resolve()
    if not source_path.is_file():
        raise ValueError(f"SQLite 來源不存在：{source_path}")

    target = Database(settings)
    target.initialize()
    if target.url.get_backend_name() == "sqlite" and target.path and target.path.resolve() == source_path:
        raise ValueError("來源與目標不可是同一個 SQLite 檔案。")

    source = create_engine(f"sqlite:///{source_path}", future=True)
    copied: dict[str, int] = {}
    try:
        with source.connect() as source_connection, target.engine.begin() as target_connection:
            source_revision = source_connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            target_revision = target_connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            if source_revision != target_revision:
                raise ValueError(f"資料庫 migration 版本不同：SQLite={source_revision}, target={target_revision}")

            occupied = [
                table.name
                for table in Base.metadata.sorted_tables
                if target_connection.execute(select(func.count()).select_from(table)).scalar_one()
            ]
            if occupied:
                raise ValueError("目標資料庫不是空的，拒絕覆寫：" + ", ".join(occupied[:5]))

            for table in Base.metadata.sorted_tables:
                rows = source_connection.execute(select(table)).mappings()
                count = 0
                while batch := rows.fetchmany(chunk_size):
                    target_connection.execute(insert(table), [dict(row) for row in batch])
                    count += len(batch)
                copied[table.name] = count

            # A heartbeat belongs to the old host process and must not make the
            # container deployment appear healthy before its worker starts.
            target_connection.execute(delete(WorkerHeartbeat))
            copied[WorkerHeartbeat.__tablename__] = 0
    except SQLAlchemyError as exc:
        raise ValueError(f"SQLite 資料匯入失敗：{exc}") from exc
    finally:
        source.dispose()
        target.engine.dispose()
    return copied
