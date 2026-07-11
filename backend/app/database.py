import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.schema import CreateColumn

from app.config import settings

logger = logging.getLogger(__name__)

settings.data_dir.mkdir(parents=True, exist_ok=True)

db_url = settings.db_url
connect_args = {}
if db_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
elif db_url.startswith("mssql"):
    connect_args = {"autocommit": False}

engine = create_engine(
    db_url,
    echo=settings.database_echo,
    future=True,
    connect_args=connect_args,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        alert_agent.record_db_connection(False)
        raise
    finally:
        db.close()


def _migrate_legacy_mssql_users() -> None:
    """SQL Server 旧库 users 表为 PascalCase 列名，统一迁移为 snake_case。"""
    if engine.dialect.name != "mssql":
        return
    insp = inspect(engine)
    table_names = {t.lower(): t for t in insp.get_table_names()}
    if "users" not in table_names:
        return

    table = table_names["users"]
    cols = {c["name"] for c in insp.get_columns(table)}
    renames = {
        "Id": "id",
        "Username": "username",
        "Email": "email",
        "Phone": "phone",
        "HashedPassword": "hashed_password",
        "IsActive": "is_active",
        "CreatedAt": "created_at",
    }
    with engine.begin() as conn:
        for old, new in renames.items():
            if old in cols and new not in cols:
                conn.execute(text(f"EXEC sp_rename '{table}.{old}', '{new}', 'COLUMN'"))
                logger.info("Renamed %s.%s -> %s", table, old, new)
                cols.discard(old)
                cols.add(new)
        if "HashedPassword" in cols and "hashed_password" in cols:
            conn.execute(text(
                f"UPDATE [{table}] SET [hashed_password] = [HashedPassword] "
                f"WHERE [HashedPassword] IS NOT NULL AND ([hashed_password] IS NULL OR [hashed_password] = '')"
            ))
            conn.execute(text(f"ALTER TABLE [{table}] DROP COLUMN [HashedPassword]"))
            logger.info("Merged and dropped duplicate HashedPassword on %s", table)


def _migrate_schema() -> None:
    """为已有数据库补充缺失列（兼容 SQL Server / SQLite）。"""
    _migrate_legacy_mssql_users()

    insp = inspect(engine)
    dialect = engine.dialect.name
    existing_tables = set(insp.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            try:
                if dialect == "mssql":
                    type_sql = col.type.compile(dialect=engine.dialect)
                    null_sql = "NULL" if col.nullable else "NOT NULL"
                    stmt = f"ALTER TABLE [{table.name}] ADD [{col.name}] {type_sql} {null_sql}"
                elif dialect == "sqlite":
                    type_sql = col.type.compile(dialect=engine.dialect)
                    stmt = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {type_sql}"
                else:
                    ddl = str(CreateColumn(col).compile(dialect=engine.dialect))
                    stmt = f"ALTER TABLE {table.name} ADD {ddl}"
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                logger.info("Added missing column %s.%s", table.name, col.name)
            except Exception as exc:
                logger.warning("Could not add column %s.%s: %s", table.name, col.name, exc)


def check_db_connection() -> bool:
    """检测数据库连接是否可用"""
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        db.close()


def _migrate_legacy_mssql_users() -> None:
    """SQL Server 旧库 users 表为 PascalCase 列名，统一迁移为 snake_case。"""
    if engine.dialect.name != "mssql":
        return
    insp = inspect(engine)
    table_names = {t.lower(): t for t in insp.get_table_names()}
    if "users" not in table_names:
        return

    table = table_names["users"]
    cols = {c["name"] for c in insp.get_columns(table)}
    renames = {
        "Id": "id",
        "Username": "username",
        "Email": "email",
        "Phone": "phone",
        "HashedPassword": "hashed_password",
        "IsActive": "is_active",
        "CreatedAt": "created_at",
    }
    with engine.begin() as conn:
        for old, new in renames.items():
            if old in cols and new not in cols:
                conn.execute(text(f"EXEC sp_rename '{table}.{old}', '{new}', 'COLUMN'"))
                logger.info("Renamed %s.%s -> %s", table, old, new)
                cols.discard(old)
                cols.add(new)
        if "HashedPassword" in cols and "hashed_password" in cols:
            conn.execute(text(
                f"UPDATE [{table}] SET [hashed_password] = [HashedPassword] "
                f"WHERE [HashedPassword] IS NOT NULL AND ([hashed_password] IS NULL OR [hashed_password] = '')"
            ))
            conn.execute(text(f"ALTER TABLE [{table}] DROP COLUMN [HashedPassword]"))
            logger.info("Merged and dropped duplicate HashedPassword on %s", table)


def _migrate_schema() -> None:
    """为已有数据库补充缺失列（兼容 SQL Server / SQLite 旧表结构）。"""
    _migrate_legacy_mssql_users()

    insp = inspect(engine)
    dialect = engine.dialect.name
    existing_tables = set(insp.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            try:
                if dialect == "mssql":
                    type_sql = col.type.compile(dialect=engine.dialect)
                    null_sql = "NULL" if col.nullable else "NOT NULL"
                    stmt = f"ALTER TABLE [{table.name}] ADD [{col.name}] {type_sql} {null_sql}"
                elif dialect == "sqlite":
                    type_sql = col.type.compile(dialect=engine.dialect)
                    stmt = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {type_sql}"
                else:
                    ddl = str(CreateColumn(col).compile(dialect=engine.dialect))
                    stmt = f"ALTER TABLE {table.name} ADD {ddl}"
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                logger.info("Added missing column %s.%s", table.name, col.name)
            except Exception as exc:
                logger.warning("Could not add column %s.%s: %s", table.name, col.name, exc)


def init_db():
    from app.models import user, records, logs, alerts  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_schema()
