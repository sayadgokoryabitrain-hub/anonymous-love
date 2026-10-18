from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse

import jwt

from core.clock import utcnow
from core.config import get_settings
from core.errors import AuthError

_PBKDF2_ROUNDS = 200_000
_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return base64.b64encode(salt + digest).decode("ascii")


def verify_password(password: str, stored: str) -> bool:
    try:
        raw = base64.b64decode(stored.encode("ascii"))
        salt, digest = raw[:16], raw[16:]
        check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
        return hmac.compare_digest(digest, check)
    except Exception:
        return False


def identity_hash(telegram_id: int) -> str:
    pepper = get_settings().identity_pepper.encode("utf-8")
    return hmac.new(pepper, str(telegram_id).encode("utf-8"), hashlib.sha256).hexdigest()


def new_referral_code() -> str:
    return "".join(secrets.choice(_REF_ALPHABET) for _ in range(8))


def new_code(length: int = 6) -> str:
    return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(length))


def issue_token(subject: str, role: str, token_type: str, hours: int, extra: dict | None = None) -> str:
    settings = get_settings()
    now = int(utcnow().timestamp())
    payload: dict[str, object] = {
        "sub": subject,
        "role": role,
        "typ": token_type,
        "iat": now,
        "exp": now + hours * 3600,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.admin_jwt_secret, algorithm="HS256")


def decode_token(token: str, expected_type: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.admin_jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AuthError("invalid_token") from exc
    if payload.get("typ") != expected_type:
        raise AuthError("invalid_token")
    return payload


def sign_provider_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_provider_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    expected = sign_provider_body(secret, body)
    return hmac.compare_digest(expected, signature.strip())


def validate_init_data(init_data: str, bot_token: str, max_age: int) -> dict:
    if not bot_token:
        raise AuthError("bot_token_missing")
    parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = parsed.pop("hash", None)
    if not received:
        raise AuthError("hash_missing")
    data_check = "\n".join(f"{key}={parsed[key]}" for key in sorted(parsed))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received):
        raise AuthError("bad_init_data")
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError as exc:
        raise AuthError("bad_init_data") from exc
    if auth_date <= 0 or int(time.time()) - auth_date > max_age:
        raise AuthError("stale_init_data")
    try:
        user = json.loads(parsed["user"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise AuthError("bad_init_data") from exc
    if "id" not in user:
        raise AuthError("bad_init_data")
    return user
