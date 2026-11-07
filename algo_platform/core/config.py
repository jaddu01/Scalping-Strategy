"""Process-level configuration read from environment variables (and an optional .env file).

Only bootstrap values live here (database URL, master key, bind address). Everything a user
can change at runtime lives in Global Settings in the database.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments. Existing env vars win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class AppConfig:
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    database_url: str = ""
    master_key: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    # "auto": Secure flag set when the request arrived over HTTPS (directly or via proxy header).
    cookie_secure: str = "auto"
    session_idle_minutes: int = 30
    session_max_hours: int = 12
    trust_proxy_headers: bool = False
    # addresses allowed to send X-Forwarded-* (the reverse proxy); "*" when the app is only reachable
    # through a proxy on a private Docker network
    forwarded_allow_ips: str = "127.0.0.1"
    disable_engine: bool = False

    @classmethod
    def from_env(cls) -> "AppConfig":
        _load_dotenv(BASE_DIR / ".env")
        data_dir = Path(os.environ.get("ALGO_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_url = os.environ.get("ALGO_DATABASE_URL") or f"sqlite:///{data_dir / 'algo.db'}"
        return cls(
            data_dir=data_dir,
            database_url=db_url,
            master_key=os.environ.get("ALGO_MASTER_KEY", ""),
            host=os.environ.get("ALGO_HOST", "127.0.0.1"),
            port=int(os.environ.get("ALGO_PORT", "8000")),
            cookie_secure=os.environ.get("ALGO_COOKIE_SECURE", "auto").lower(),
            session_idle_minutes=int(os.environ.get("ALGO_SESSION_IDLE_MINUTES", "30")),
            session_max_hours=int(os.environ.get("ALGO_SESSION_MAX_HOURS", "12")),
            trust_proxy_headers=_bool(os.environ.get("ALGO_TRUST_PROXY_HEADERS"), False),
            forwarded_allow_ips=os.environ.get("ALGO_FORWARDED_ALLOW_IPS", "127.0.0.1"),
            disable_engine=_bool(os.environ.get("ALGO_DISABLE_ENGINE"), False),
        )

    @property
    def log_dir(self) -> Path:
        p = self.data_dir / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cache_dir(self) -> Path:
        p = self.data_dir / "cache"
        p.mkdir(parents=True, exist_ok=True)
        return p
