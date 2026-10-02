"""Single-owner password hashing, using stdlib scrypt (OWASP parameters)."""
import hashlib
import secrets

N, R, P = 2**17, 8, 1


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=N, r=R, p=P,
                          dklen=64, maxmem=256 * 1024 * 1024)


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must be between 12 and 1024 characters")
    salt = secrets.token_bytes(16)
    return f"scrypt:{N}:{R}:{P}:{salt.hex()}:{_derive(password, salt).hex()}"


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split(":")
    if len(parts) != 6 or parts[:4] != ["scrypt", str(N), str(R), str(P)]:
        raise ValueError("Invalid password hash configuration")
    salt, expected = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    if len(salt) != 16 or len(expected) != 64:
        raise ValueError("Invalid password hash configuration")
    return secrets.compare_digest(_derive(password, salt), expected)
