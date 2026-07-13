from app.models.user import User
from app.utils.crypto import blind_index, decrypt_text, encrypt_text


def normalize_email(email: str) -> str:
    return email.strip().lower()


def protect_email(email: str) -> dict[str, str]:
    normalized = normalize_email(email)
    lookup = blind_index(normalized, purpose="email-lookup")
    return {
        # 历史 email 列有唯一约束，保存摘要可继续利用该约束且不泄露原文。
        "email": lookup,
        "email_lookup": lookup,
        "email_encrypted": encrypt_text(normalized),
    }


def protect_phone(phone: str) -> dict[str, str]:
    normalized = phone.strip()
    lookup = blind_index(normalized, purpose="phone-lookup")
    return {
        # 历史 phone 列最多 20 字符，使用截断摘要维持旧唯一索引。
        "phone": lookup[:20],
        "phone_lookup": lookup,
        "phone_encrypted": encrypt_text(normalized),
    }


def email_lookup(email: str) -> str:
    return blind_index(normalize_email(email), purpose="email-lookup")


def user_email(user: User) -> str | None:
    if user.email_encrypted:
        return decrypt_text(user.email_encrypted)
    # 仅供 setup_security.py 执行迁移前的短暂兼容。
    return user.email if user.email and "@" in user.email else None


def user_phone(user: User) -> str | None:
    if user.phone_encrypted:
        return decrypt_text(user.phone_encrypted)
    return user.phone
