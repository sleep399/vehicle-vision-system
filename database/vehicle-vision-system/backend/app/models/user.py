from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, text
from app.database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index(
            "ix_users_phone",
            "phone",
            unique=True,
            mssql_where=text("phone IS NOT NULL"),
            sqlite_where=text("phone IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    # email/phone 仅保留历史兼容列；新数据中写入 HMAC 摘要，不再写明文。
    email = Column(String(128), unique=True, index=True, nullable=True)
    phone = Column(String(20), nullable=True)
    email_encrypted = Column(String(512), nullable=True)
    email_lookup = Column(String(64), nullable=True, index=True)
    phone_encrypted = Column(String(256), nullable=True)
    phone_lookup = Column(String(64), nullable=True, index=True)
    hashed_password = Column(String(256), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class VerificationCode(Base):
    __tablename__ = "verification_codes"

    id = Column(Integer, primary_key=True, index=True)
    target = Column(String(128), index=True, nullable=False)
    code = Column(String(8), nullable=False)
    target_lookup = Column(String(64), nullable=True, index=True)
    code_hash = Column(String(64), nullable=True)
    purpose = Column(String(32), default="login")
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)


class WechatLoginSession(Base):
    __tablename__ = "wechat_login_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(64), unique=True, index=True)
    status = Column(String(16), default="pending")
    user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
