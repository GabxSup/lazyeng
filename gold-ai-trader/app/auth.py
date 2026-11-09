"""Minimal signed-cookie authentication for the single-operator dashboard."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

COOKIE_NAME = "aurum_session"
SESSION_SECONDS = 12 * 60 * 60
MAX_LOGIN_ATTEMPTS = 6
LOGIN_WINDOW_SECONDS = 300


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class AuthManager:
    def __init__(self, password: str, secret: str, cookie_secure: bool = False) -> None:
        self.password = password or ""
        self.secret = secret.encode("utf-8")
        self.cookie_secure = cookie_secure
        self.enabled = bool(self.password)
        self._attempts: Dict[str, list] = {}
        self._lock = threading.Lock()

    def _sign(self, payload: bytes) -> str:
        return _b64encode(hmac.new(self.secret, payload, hashlib.sha256).digest())

    def issue(self) -> str:
        payload = json.dumps({"exp": int(time.time()) + SESSION_SECONDS, "n": secrets.token_urlsafe(12)}, separators=(",", ":")).encode("utf-8")
        return _b64encode(payload) + "." + self._sign(payload)

    def valid(self, token: Optional[str]) -> bool:
        if not token or "." not in token:
            return False
        encoded, signature = token.rsplit(".", 1)
        try:
            payload = _b64decode(encoded)
        except Exception:
            return False
        expected = self._sign(payload)
        if not hmac.compare_digest(signature, expected):
            return False
        try:
            data = json.loads(payload.decode("utf-8"))
            return int(data.get("exp", 0)) > int(time.time())
        except Exception:
            return False

    def login_allowed(self, ip: str) -> Tuple[bool, int]:
        now = time.time()
        with self._lock:
            attempts = [ts for ts in self._attempts.get(ip, []) if now - ts < LOGIN_WINDOW_SECONDS]
            self._attempts[ip] = attempts
            if len(attempts) >= MAX_LOGIN_ATTEMPTS:
                return False, int(LOGIN_WINDOW_SECONDS - (now - attempts[0]))
            return True, 0

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._attempts.setdefault(ip, []).append(time.time())

    def clear_failures(self, ip: str) -> None:
        with self._lock:
            self._attempts.pop(ip, None)

    def check_password(self, candidate: str) -> bool:
        if not self.enabled:
            return True
        return hmac.compare_digest(str(candidate or ""), self.password)

    def cookie_header(self, token: str) -> str:
        attributes = [
            f"{COOKIE_NAME}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={SESSION_SECONDS}",
        ]
        if self.cookie_secure:
            attributes.append("Secure")
        return "; ".join(attributes)

    def clear_cookie_header(self) -> str:
        attributes = [f"{COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=0"]
        if self.cookie_secure:
            attributes.append("Secure")
        return "; ".join(attributes)


def parse_cookies(header: Optional[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not header:
        return result
    for chunk in header.split(";"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            result[key.strip()] = value.strip()
    return result
