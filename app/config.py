from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    database_url: str
    secret_key: str
    token_ttl_minutes: int
    seed_demo: bool
    generation_delay_ms: int
    heartbeat_seconds: int
    llm_provider: str
    llm_api_key: str
    llm_model: str


def load_settings() -> Settings:
    default_db = f"sqlite:///{(BASE_DIR / 'data' / 'trace.db').as_posix()}"
    return Settings(
        database_url=os.getenv("DATABASE_URL", default_db),
        secret_key=os.getenv("SECRET_KEY", "dev-only-secret-change-me"),
        token_ttl_minutes=_int("TOKEN_TTL_MINUTES", 480),
        seed_demo=os.getenv("SEED_DEMO", "1") == "1",
        generation_delay_ms=_int("GENERATION_DELAY_MS", 120),
        heartbeat_seconds=_int("HEARTBEAT_SECONDS", 15),
        llm_provider=os.getenv("LLM_PROVIDER", "rules").lower(),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", ""),
    )


settings = load_settings()
