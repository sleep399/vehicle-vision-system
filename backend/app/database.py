from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

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
    finally:
        db.close()


def init_db():
    from app.models import user, records, logs, alerts  # noqa: F401

    Base.metadata.create_all(bind=engine)
