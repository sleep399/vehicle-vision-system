import json
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.records import LicensePlateRecord
from app.models.user import User, VerificationCode
from app.utils.auth import hash_password
from app.utils.crypto import (
    ENCRYPTED_PREFIX,
    decrypt_json,
    decrypt_text,
    encrypt_json,
    hash_verification_code,
)
from app.utils.privacy import email_lookup, protect_email, protect_phone


def _decrypt_with_keys(value: str, previous_keys: list[str]) -> str | None:
    for key_text in [None, *previous_keys]:
        try:
            return decrypt_text(value, key_text=key_text)
        except Exception:
            continue
    return None


def _decrypt_json_with_keys(value: str, previous_keys: list[str]) -> dict | None:
    for key_text in [None, *previous_keys]:
        try:
            return decrypt_json(value, key_text=key_text)
        except Exception:
            continue
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def migration_needed(db: Session) -> bool:
    for user in db.query(User).all():
        if user.hashed_password and not user.hashed_password.startswith("$2"):
            return True
        if user.email and "@" in user.email:
            return True
        if user.phone and not user.phone_encrypted:
            return True
        if user.email_encrypted and not user.email_encrypted.startswith(ENCRYPTED_PREFIX):
            return True
    for item in db.query(VerificationCode).all():
        if not item.target_lookup or not item.code_hash or item.code != "hashed":
            return True
    return db.query(LicensePlateRecord).filter(
        ~LicensePlateRecord.plates_json.startswith(ENCRYPTED_PREFIX)
    ).first() is not None


def validate_migration_keys(db: Session, previous_keys: list[str] | None = None) -> None:
    """在写库前确认新旧密钥至少有一把能读取全部现有 AES 数据。"""
    keys = [key for key in (previous_keys or []) if key]
    for user in db.query(User).all():
        for field_name in ("email_encrypted", "phone_encrypted"):
            value = getattr(user, field_name)
            if value and _decrypt_with_keys(value, keys) is None:
                raise RuntimeError(f"无法解密用户 {user.id} 的隐私数据，请恢复原 .env 后重试")
    for record in db.query(LicensePlateRecord).all():
        if _decrypt_json_with_keys(record.plates_json, keys) is None:
            raise RuntimeError(f"无法解密车牌记录 {record.id}，请恢复原 .env 后重试")


def write_encrypted_backup(db: Session, backup_dir: Path) -> Path:
    """备份本次迁移会改动的字段，备份文件本身也使用新 AES 密钥加密。"""
    payload = {
        "created_at": datetime.utcnow().isoformat(),
        "users": [
            {
                "id": row.id,
                "email": row.email,
                "phone": row.phone,
                "email_encrypted": row.email_encrypted,
                "email_lookup": row.email_lookup,
                "phone_encrypted": row.phone_encrypted,
                "phone_lookup": row.phone_lookup,
                "hashed_password": row.hashed_password,
            }
            for row in db.query(User).all()
        ],
        "verification_codes": [
            {
                "id": row.id,
                "target": row.target,
                "code": row.code,
                "target_lookup": row.target_lookup,
                "code_hash": row.code_hash,
            }
            for row in db.query(VerificationCode).all()
        ],
        "license_plate_records": [
            {"id": row.id, "plates_json": row.plates_json}
            for row in db.query(LicensePlateRecord).all()
        ],
    }
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"security-migration-{datetime.now():%Y%m%d-%H%M%S}.json.enc"
    path.write_text(encrypt_json(payload), encoding="utf-8")
    return path


def migrate_sensitive_data(db: Session, previous_keys: list[str] | None = None) -> dict[str, int]:
    previous_keys = [key for key in (previous_keys or []) if key]
    counts = {"users": 0, "passwords": 0, "verification_codes": 0, "plate_records": 0}

    try:
        for user in db.query(User).all():
            changed = False
            if user.hashed_password and not user.hashed_password.startswith("$2"):
                user.hashed_password = hash_password(user.hashed_password)
                counts["passwords"] += 1
                changed = True

            email = None
            if user.email_encrypted:
                email = _decrypt_with_keys(user.email_encrypted, previous_keys)
            elif user.email and "@" in user.email:
                email = user.email
            if email:
                fields = protect_email(email)
                for name, value in fields.items():
                    if getattr(user, name) != value:
                        setattr(user, name, value)
                        changed = True

            phone = None
            if user.phone_encrypted:
                phone = _decrypt_with_keys(user.phone_encrypted, previous_keys)
            elif user.phone:
                phone = user.phone
            if phone:
                fields = protect_phone(phone)
                for name, value in fields.items():
                    if getattr(user, name) != value:
                        setattr(user, name, value)
                        changed = True

            if changed:
                counts["users"] += 1

        for item in db.query(VerificationCode).all():
            if item.target_lookup and item.code_hash and item.code == "hashed":
                continue
            if "@" not in (item.target or "") or not item.code or item.code == "hashed":
                continue
            email = item.target.strip().lower()
            lookup = email_lookup(email)
            item.target_lookup = lookup
            item.code_hash = hash_verification_code(email, item.purpose, item.code)
            item.target = lookup
            item.code = "hashed"
            counts["verification_codes"] += 1

        for record in db.query(LicensePlateRecord).all():
            data = _decrypt_json_with_keys(record.plates_json, previous_keys)
            if data is None:
                raise RuntimeError(f"无法解密车牌记录 id={record.id}，迁移已回滚")
            try:
                decrypt_json(record.plates_json)
                already_current = record.plates_json.startswith(ENCRYPTED_PREFIX)
            except Exception:
                already_current = False
            if not already_current:
                record.plates_json = encrypt_json(data)
                counts["plate_records"] += 1

        db.commit()
        return counts
    except Exception:
        db.rollback()
        raise
