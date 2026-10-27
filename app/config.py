"""Application configuration. Secrets come from the environment, never the file.

A token in a config file is a token in a backup, a screenshot, and a `git add -A`.
So the config file holds only what is safe to commit, and the two secrets
(`TELEGRAM_BOT_TOKEN`, and the model key) are read from the environment. The
`describe()` output used by diagnostics is redacted by construction: it renders
the *status* of each secret ("set"/"missing"), never a value.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_ids(name: str) -> tuple[int, ...]:
    raw = os.environ.get(name, "")
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return tuple(ids)


@dataclass
class AppConfig:
    """Everything the bot needs at startup. Constructed once."""

    # -- identity / scope --------------------------------------------------
    bot_name: str = "Alma"
    bot_username: str = ""          # without the leading @; used to detect mentions
    allowed_chat_ids: tuple[int, ...] = ()
    admin_user_ids: tuple[int, ...] = ()

    # -- telegram ----------------------------------------------------------
    telegram_token: str = ""
    poll_timeout: int = 25
    dry_run: bool = False

    # -- behaviour ---------------------------------------------------------
    proactive_enabled: bool = False
    debounce_seconds: float = 3.5
    max_turn_messages: int = 12
    history_messages: int = 24

    # -- proactive rate limiting ------------------------------------------
    proactive_min_interval_seconds: float = 240.0
    proactive_max_per_hour: int = 4
    proactive_min_confidence: float = 0.62

    # -- runtime -----------------------------------------------------------
    workspace: str = ""
    db_path: str = ""
    provider: str = "magpie"
    base_url: str = ""
    model: str = ""
    model_api_key: str = ""
    context_limit: int = 40_000
    request_timeout: float = 60.0
    system_prompt_path: str = ""

    # -- diagnostics -------------------------------------------------------
    log_level: str = "INFO"

    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- derived -----------------------------------------------------------

    @property
    def is_configured_for_live(self) -> bool:
        return bool(self.telegram_token) and bool(self.allowed_chat_ids)

    def allows_chat(self, chat_id: int) -> bool:
        return chat_id in self.allowed_chat_ids

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_user_ids

    def describe(self) -> dict[str, Any]:
        """Redacted view, safe to log and to print in a diagnostics report."""
        return {
            "bot_name": self.bot_name,
            "bot_username": self.bot_username or "(unset)",
            "allowed_chat_ids": list(self.allowed_chat_ids),
            "admin_user_ids": list(self.admin_user_ids),
            "telegram_token": "set" if self.telegram_token else "missing",
            "model_api_key": "set" if self.model_api_key else "missing",
            "dry_run": self.dry_run,
            "proactive_enabled": self.proactive_enabled,
            "debounce_seconds": self.debounce_seconds,
            "history_messages": self.history_messages,
            "proactive_min_interval_seconds": self.proactive_min_interval_seconds,
            "proactive_max_per_hour": self.proactive_max_per_hour,
            "provider": self.provider,
            "base_url": self.base_url or "(provider default)",
            "model": self.model or "(unset)",
            "context_limit": self.context_limit,
            "request_timeout": self.request_timeout,
            "workspace": self.workspace,
            "db_path": self.db_path,
        }


def load_config(
    path: Path | str | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> AppConfig:
    """File first, environment second. Environment always wins for secrets."""
    env = os.environ if environ is None else environ
    file_data: dict[str, Any] = {}
    target = Path(path) if path else _default_config_path(env)
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                file_data = loaded
        except (OSError, json.JSONDecodeError):
            file_data = {}

    config = AppConfig()
    config.raw = file_data

    # Non-secret values may come from the file.
    for key in (
        "bot_name", "bot_username", "poll_timeout", "debounce_seconds",
        "max_turn_messages", "history_messages", "proactive_min_interval_seconds",
        "proactive_max_per_hour", "proactive_min_confidence", "workspace",
        "db_path", "provider", "base_url", "model", "context_limit",
        "request_timeout", "system_prompt_path", "log_level",
    ):
        if key in file_data and file_data[key] not in (None, ""):
            setattr(config, key, type(getattr(config, key))(file_data[key]))

    for key in ("dry_run", "proactive_enabled"):
        if key in file_data:
            setattr(config, key, bool(file_data[key]))

    for key in ("allowed_chat_ids", "admin_user_ids"):
        if key in file_data and isinstance(file_data[key], list):
            values = []
            for item in file_data[key]:
                try:
                    values.append(int(item))
                except (TypeError, ValueError):
                    continue
            setattr(config, key, tuple(values))

    # Environment overrides.
    config.telegram_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    config.allowed_chat_ids = _env_ids("TELEGRAM_ALLOWED_CHAT_IDS") or config.allowed_chat_ids
    config.admin_user_ids = _env_ids("TELEGRAM_ADMIN_USER_IDS") or config.admin_user_ids
    config.bot_username = env.get("TELEGRAM_BOT_USERNAME", config.bot_username).lstrip("@")
    config.bot_name = env.get("BOT_NAME", config.bot_name)
    config.poll_timeout = _env_int("TELEGRAM_POLL_TIMEOUT", config.poll_timeout)
    config.dry_run = _env_bool("BOT_DRY_RUN", config.dry_run)
    config.proactive_enabled = _env_bool("BOT_PROACTIVE", config.proactive_enabled)
    config.debounce_seconds = _env_float("BOT_DEBOUNCE_SECONDS", config.debounce_seconds)
    config.history_messages = _env_int("BOT_HISTORY_MESSAGES", config.history_messages)
    config.proactive_min_interval_seconds = _env_float(
        "BOT_PROACTIVE_MIN_INTERVAL", config.proactive_min_interval_seconds
    )
    config.proactive_max_per_hour = _env_int(
        "BOT_PROACTIVE_MAX_PER_HOUR", config.proactive_max_per_hour
    )
    config.proactive_min_confidence = _env_float(
        "BOT_PROACTIVE_MIN_CONFIDENCE", config.proactive_min_confidence
    )

    # Model provider: reuse Nervipulsa's own environment names where they exist.
    config.provider = env.get("NERVIPULSA_PROVIDER", config.provider)
    config.base_url = env.get("NERVIPULSA_BASE_URL", config.base_url)
    config.model = env.get("NERVIPULSA_MODEL", config.model)
    config.model_api_key = env.get(
        "NERVIPULSA_API_KEY", env.get("OPENAI_API_KEY", "")
    ).strip()
    config.context_limit = _env_int("NERVIPULSA_CONTEXT_LIMIT", config.context_limit)
    config.request_timeout = _env_float("NERVIPULSA_TIMEOUT", config.request_timeout)

    root = Path(config.workspace) if config.workspace else _default_workspace(env)
    config.workspace = str(root)
    if not config.db_path:
        config.db_path = str(root / "bot.sqlite3")
    if not config.system_prompt_path:
        config.system_prompt_path = str(
            Path(__file__).resolve().parent / "prompts" / "group_agent.md"
        )
    return config


def _default_config_path(env: dict[str, str] | os._Environ) -> Path:
    override = env.get("BOT_CONFIG")
    if override:
        return Path(override)
    return Path("config.local.json")


def _default_workspace(env: dict[str, str] | os._Environ) -> Path:
    override = env.get("BOT_WORKSPACE")
    if override:
        return Path(override)
    base = env.get("LOCALAPPDATA") or env.get("XDG_STATE_HOME") or str(Path.home())
    return Path(base) / "nervipulsa-telegram-bot"


def load_system_prompt(path: str | Path) -> str:
    """Read the agent profile. A missing profile is a startup error, not a default.

    Shipping a fallback prompt would mean the bot silently runs with an identity
    nobody reviewed, which is exactly the failure this file exists to prevent.
    """
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"system prompt file is empty: {target}")
    return text
