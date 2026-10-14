from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.path = Path(settings.data_dir) / "ecourse.sqlite3"
        self.engine: Engine = create_engine(f"sqlite:///{self.path}", future=True)
        event.listen(self.engine, "connect", self._configure_sqlite)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def initialize(self) -> None:
        """Explicit schema migration. Application startup must not call this implicitly."""
        Path(self.settings.data_dir).mkdir(parents=True, exist_ok=True)
        try:
            from alembic import command
            from alembic.config import Config
        except ImportError as exc:
            raise RuntimeError("Alembic 尚未安裝，無法初始化資料庫。") from exc
        root = Path(__file__).resolve().parents[3]
        packaged_migrations = Path(__file__).resolve().parent / "migrations"
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(packaged_migrations))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{self.path}")
        command.upgrade(config, "head")

    def ready(self) -> bool:
        if not self.path.exists():
            return False
        try:
            with self.engine.connect() as connection:
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
                connection.execute(text("SELECT 1"))
            return revision is not None
        except Exception:
            return False

    @contextmanager
    def session(self):
        session: Session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


__all__ = ["Base", "Database"]
