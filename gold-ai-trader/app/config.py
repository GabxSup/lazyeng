"""Configuration loading for AURUM AI.

The application has no third-party runtime dependencies, so this module also
implements a deliberately small .env reader. Environment variables always win
over values written in .env.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from .util import normalise_symbol

APP_NAME = "AURUM AI"
APP_VERSION = "1.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_env(project_root: Path = PROJECT_ROOT) -> None:
    """Load .env without overwriting variables already set by the process."""
    env_file = Path(os.environ.get("AURUM_ENV_FILE", str(project_root / ".env")))
    for key, value in _parse_dotenv(env_file).items():
        os.environ.setdefault(key, value)


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


@dataclass(frozen=True)
class Config:
    project_root: Path = PROJECT_ROOT
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    host: str = "127.0.0.1"
    port: int = 8080
    dashboard_password: str = ""
    session_secret: str = ""
    cookie_secure: bool = False
    live_enabled: bool = False
    live_arm_phrase: str = "ARM LIVE TRADING"
    bridge_url: str = "http://127.0.0.1:8765"
    bridge_token: str = ""
    ea_token: str = ""
    bridge_timeout: float = 6.0
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_timeout: float = 12.0
    paper_balance: float = 10000.0
    symbol: str = "XAUUSD"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, project_root: Optional[Path] = None) -> "Config":
        root = Path(project_root) if project_root else PROJECT_ROOT
        load_env(root)
        data_dir = Path(_str("AURUM_DATA_DIR", str(root / "data"))).expanduser()
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
        ai_key = _str("AURUM_AI_API_KEY") or _str("OPENAI_API_KEY")
        session_secret = _str("AURUM_SECRET_KEY") or secrets.token_hex(32)
        return cls(
            project_root=root,
            data_dir=data_dir,
            host=_str("AURUM_HOST", "127.0.0.1"),
            port=_int("AURUM_PORT", 8080, 1, 65535),
            dashboard_password=_str("AURUM_DASHBOARD_PASSWORD"),
            session_secret=session_secret,
            cookie_secure=_bool("AURUM_COOKIE_SECURE", False),
            live_enabled=_bool("AURUM_LIVE_ENABLED", False),
            live_arm_phrase=_str("AURUM_LIVE_ARM_PHRASE", "ARM LIVE TRADING"),
            bridge_url=_str("AURUM_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/"),
            bridge_token=_str("AURUM_BRIDGE_TOKEN"),
            ea_token=_str("AURUM_EA_TOKEN"),
            bridge_timeout=float(_int("AURUM_BRIDGE_TIMEOUT", 6, 1, 60)),
            ai_api_key=ai_key,
            ai_base_url=_str("AURUM_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            ai_model=_str("AURUM_AI_MODEL", "gpt-4o-mini"),
            ai_timeout=float(_int("AURUM_AI_TIMEOUT", 12, 2, 60)),
            paper_balance=float(_str("AURUM_PAPER_BALANCE", "10000") or "10000"),
            symbol=normalise_symbol(_str("AURUM_SYMBOL", "XAUUSD")),
            log_level=_str("AURUM_LOG_LEVEL", "INFO").upper(),
        )

    @property
    def auth_enabled(self) -> bool:
        return bool(self.dashboard_password)

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_api_key and self.ai_model)

    @property
    def bridge_configured(self) -> bool:
        return bool(self.bridge_url and self.bridge_token)

    def public_connection(self) -> Dict[str, object]:
        return {
            "bridge_configured": self.bridge_configured,
            "bridge_host": _url_host(self.bridge_url),
            "ea_configured": bool(self.ea_token),
            "ea_endpoint": "http://127.0.0.1/api/ea/poll",
            "live_enabled": self.live_enabled,
            "ai_configured": self.ai_configured,
            "ai_model": self.ai_model if self.ai_configured else None,
        }


def _url_host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.netloc or url
    except Exception:
        return "configured"


_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config
