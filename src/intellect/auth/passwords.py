"""版本化密码哈希协议，不记录原始密码。"""

import hashlib
import hmac
import secrets

ITERATIONS = 600_000


def hash_password(password: str) -> str:
    """为非空密码生成随机盐及 PBKDF2-SHA256 哈希。"""
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """验证固定版本的密码哈希，损坏或未知格式一律失败。"""
    if not isinstance(password, str) or not password:
        return False
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or rounds != str(ITERATIONS):
            return False
        salt_bytes, expected_bytes = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes, ITERATIONS)
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, AttributeError):
        return False
