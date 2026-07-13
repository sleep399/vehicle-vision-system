import base64
import hashlib
import hmac
import json
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app.config import settings


ENCRYPTED_PREFIX = "enc:v1:"


def _get_aes_key(key_text: str | None = None) -> bytes:
    """解析 256 位 AES 密钥；显式 key_text 也兼容迁移旧的文本密钥。"""
    value = (key_text if key_text is not None else settings.aes_key).strip()
    if len(value) == 64:
        try:
            return bytes.fromhex(value)
        except ValueError as exc:
            raise RuntimeError("AES_KEY 必须是 setup_security.py 生成的 64 位十六进制字符串") from exc
    if key_text is not None and value:
        # 仅供一次性迁移历史版本使用；新配置不允许回退到可猜测文本密钥。
        return hashlib.sha256(value.encode("utf-8")).digest()
    raise RuntimeError("尚未配置安全的 AES_KEY，请先运行 python setup_security.py")


def validate_aes_key() -> None:
    _get_aes_key()


def encrypt_text(plain: str, *, key_text: str | None = None) -> str:
    key = _get_aes_key(key_text)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plain.encode("utf-8"), None)
    return ENCRYPTED_PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_text(cipher: str, *, key_text: str | None = None) -> str:
    key = _get_aes_key(key_text)
    payload = cipher[len(ENCRYPTED_PREFIX):] if cipher.startswith(ENCRYPTED_PREFIX) else cipher
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
    except Exception as exc:
        raise ValueError("无效的加密数据") from exc
    if len(raw) < 29:
        raise ValueError("无效的加密数据")
    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


def is_encrypted(value: str | None) -> bool:
    return bool(value and value.startswith(ENCRYPTED_PREFIX))


def blind_index(value: str, *, purpose: str = "privacy-lookup", key_text: str | None = None) -> str:
    """生成可查询但不可还原的 HMAC 摘要。"""
    key = _get_aes_key(key_text)
    derived = hmac.new(key, purpose.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(derived, value.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_verification_code(email: str, purpose: str, code: str, *, key_text: str | None = None) -> str:
    normalized = email.strip().lower()
    return blind_index(
        f"{purpose}:{normalized}:{code}",
        purpose="verification-code",
        key_text=key_text,
    )


def encrypt_json(data: dict, *, key_text: str | None = None) -> str:
    return encrypt_text(json.dumps(data, ensure_ascii=False), key_text=key_text)


def decrypt_json(cipher: str, *, key_text: str | None = None) -> dict:
    return json.loads(decrypt_text(cipher, key_text=key_text))
