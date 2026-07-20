# auth.py — team-only viewer auth: one shared password -> HMAC-signed cookie.
#
# No user database. A valid cookie is `<b64(exp)>.<hmac(secret, b64(exp))>`; we
# verify the signature and expiry on every request. Ingest uses a separate bearer
# token (see main.py). Stdlib only.

import base64
import hmac
import time
from hashlib import sha256

import settings


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(settings.SECRET_KEY.encode(), payload, sha256).digest())


def make_cookie() -> str:
    payload = str(int(time.time()) + settings.SESSION_TTL).encode()
    return f"{_b64(payload)}.{_sign(payload)}"


def valid_cookie(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    try:
        b, sig = token.split(".", 1)
        payload = _unb64(b)
        if not hmac.compare_digest(sig, _sign(payload)):
            return False
        return int(payload.decode()) > time.time()
    except (ValueError, TypeError):
        return False


def check_password(pw: str) -> bool:
    return bool(settings.VIEW_PASSWORD) and hmac.compare_digest(pw, settings.VIEW_PASSWORD)


def check_ingest_token(header: str | None) -> bool:
    """Validate an `Authorization: Bearer <token>` header for ingest."""
    if not settings.INGEST_TOKEN or not header:
        return False
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1], settings.INGEST_TOKEN)
