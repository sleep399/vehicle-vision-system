from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.records import LicensePlateRecord
from app.models.user import User, VerificationCode
from app.security_migration import (
    migrate_sensitive_data,
    migration_needed,
    write_encrypted_backup,
)
from app.utils.auth import verify_password
from app.utils.crypto import decrypt_json, decrypt_text, encrypt_json, encrypt_text


def test_aes_gcm_round_trip_uses_a_fresh_nonce():
    first = encrypt_text("private@example.com")
    second = encrypt_text("private@example.com")

    assert first != second
    assert decrypt_text(first) == "private@example.com"
    assert decrypt_text(second) == "private@example.com"


def test_plaintext_is_not_accepted_as_a_password_hash():
    assert verify_password("admin123", "admin123") is False


def test_sql_seed_contains_only_bcrypt_admin_password():
    sql_path = Path(__file__).resolve().parents[4] / "SQLQuery1.sql"
    sql = sql_path.read_text(encoding="utf-8")

    assert "N'admin123'" not in sql
    assert "$2b$12$" in sql


def test_legacy_sensitive_data_is_backed_up_and_migrated(tmp_path):
    local_engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    Base.metadata.create_all(local_engine)
    LocalSession = sessionmaker(bind=local_engine)
    old_key = "legacy-development-key"

    with LocalSession() as db:
        db.add(User(
            username="legacy_user",
            email="legacy@example.com",
            phone="13800000000",
            hashed_password="old-password",
        ))
        db.add(VerificationCode(
            target="legacy@example.com",
            code="123456",
            purpose="login",
            expires_at=datetime.utcnow(),
        ))
        db.add(LicensePlateRecord(
            source_type="image",
            plates_json=encrypt_json({"plates": [{"plate_number": "京A12345"}]}, key_text=old_key),
        ))
        db.commit()

        assert migration_needed(db) is True
        backup = write_encrypted_backup(db, tmp_path / "backups")
        assert "legacy@example.com" not in backup.read_text(encoding="utf-8")

        counts = migrate_sensitive_data(db, previous_keys=[old_key])
        assert counts == {"users": 1, "passwords": 1, "verification_codes": 1, "plate_records": 1}
        assert migration_needed(db) is False

        user = db.query(User).one()
        assert "legacy@example.com" not in (user.email or "")
        assert decrypt_text(user.email_encrypted) == "legacy@example.com"
        assert user.hashed_password.startswith("$2")
        assert verify_password("old-password", user.hashed_password)

        code = db.query(VerificationCode).one()
        assert code.target != "legacy@example.com"
        assert code.code == "hashed"
        assert code.code_hash

        plate = db.query(LicensePlateRecord).one()
        assert decrypt_json(plate.plates_json)["plates"][0]["plate_number"] == "京A12345"

    local_engine.dispose()


def test_security_setup_helpers_are_idempotent(tmp_path):
    from setup_security import generate_localhost_certificate, read_env, update_env

    env_path = tmp_path / ".env"
    update_env(env_path, {"AES_KEY": "22" * 32, "HTTPS_CERTFILE": "cert.pem"})
    update_env(env_path, {"AES_KEY": "22" * 32, "HTTPS_CERTFILE": "cert.pem"})
    assert read_env(env_path)["AES_KEY"] == "22" * 32
    assert env_path.read_text(encoding="utf-8").count("AES_KEY=") == 1

    cert_path = tmp_path / "localhost-cert.pem"
    key_path = tmp_path / "localhost-key.pem"
    assert generate_localhost_certificate(cert_path, key_path) is True
    assert generate_localhost_certificate(cert_path, key_path) is False
