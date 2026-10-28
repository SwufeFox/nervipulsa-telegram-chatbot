"""Application configuration. Secrets come from the environment, never the file.

A token in a config file is a token in a backup, a screenshot, and a `git add -A`.
So the config file holds only what is safe to commit, and the two secrets
(`TELEGRAM_BOT_TOKEN`, and the model key) are read from the environment. The
`describe()` output used by diagnostics is redacted by construction: it renders
the *status* of each secret ("set"/"missing"), never a value.

Two file formats are accepted, chosen by suffix:

* `.toml` — the format this project recommends. Sections are documentation:
  `[bot]`, `[telegram]`, `[model]`, `[runtime]`, `[behaviour]`, `[proactive]`.
* `.json` — the original flat format, still supported so existing deployments
  do not break on upgrade.

Section names are cosmetic. Only the key names matter, so `telegram.poll_timeout`
and a top-level `poll_timeout` are the same setting.
"""
from __future__ import annotations

import json
import os
import tomllib
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

    # -- autonomous participation (the default path) -----------------------
    # These are cost/latency controls only. None of them inspects message
    # content to decide whether the agent "deserves" to think: they decide
    # when an opportunity is delivered, and the agent decides what to do.
    idle_seconds: float = 2.0
    max_batch_seconds: float = 15.0
    max_batch_observations: int = 24
    # Resource limits, not social judgements: they bound how often the model
    # may be asked to look and how often the bot may speak. Exhausting one
    # defers the turn (the observations stay queued); it never becomes a
    # recorded decision that the agent chose silence.
    max_activations_per_hour: int = 60
    max_actions_per_hour: int = 20
    min_action_interval_seconds: float = 20.0

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
    # The model view and the reply budget are both "effectively unlimited":
    # 1M tokens each. The earlier 40k/4096 pair was the binding constraint, not
    # the model — Muse Spark spends ~98% of its output on reasoning, so a 4096
    # cap meant a complex question could return nothing at all after burning
    # the entire budget thinking (measured in docs/LONG_CONVERSATION.md).
    #
    # "Unlimited" here means "high enough never to bind", not a real infinity:
    # the provider still enforces its own limit and will reject a request it
    # considers too large. Both remain overridable.
    context_limit: int = 1_000_000
    max_output_tokens: int = 1_000_000
    # A 1M-token reply can take far longer than the old 60s. This is a client
    # HTTP timeout, so leaving it at 60s would abort a request the model was
    # still legitimately working on.
    request_timeout: float = 900.0
    # Deprecated single-file profile. Kept so an existing config file (or a
    # BOT_SYSTEM_PROMPT_PATH) still works; when set it replaces both halves.
    system_prompt_path: str = ""
    # The agent profile is two files, composed in this order:
    #   soul.md          who the bot is — identity, hard limits, untrusted input
    #   introduction.md  how it takes part — when to speak, tone, length
    # Soul is the part you must not weaken; introduction is the part you tune.
    # They are separate so an operator can change behaviour without touching the
    # rules that keep the bot honest, and vice versa.
    soul_path: str = ""
    introduction_path: str = ""

    # -- diagnostics -------------------------------------------------------
    log_level: str = "INFO"
    # Characters of message text to show in a log preview. 0 (the default)
    # keeps message content out of the terminal entirely; any positive value is
    # a deliberate choice by the operator.
    log_summary_chars: int = 0
    # How often the periodic status block is printed, in seconds. 0 disables it.
    log_status_seconds: float = 60.0

    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    # Keys present in the config file that no setting consumes. Surfaced by
    # `--check` so a typo is a visible error, not a silent no-op.
    unknown_keys: tuple[str, ...] = ()

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
            "idle_seconds": self.idle_seconds,
            "max_batch_seconds": self.max_batch_seconds,
            "max_batch_observations": self.max_batch_observations,
            "max_activations_per_hour": self.max_activations_per_hour,
            "max_actions_per_hour": self.max_actions_per_hour,
            "min_action_interval_seconds": self.min_action_interval_seconds,
            "log_summary_chars": self.log_summary_chars,
            "log_status_seconds": self.log_status_seconds,
            "proactive_min_interval_seconds": self.proactive_min_interval_seconds,
            "proactive_max_per_hour": self.proactive_max_per_hour,
            "provider": self.provider,
            "base_url": self.base_url or "(provider default)",
            "model": self.model or "(unset)",
            "context_limit": self.context_limit,
            "max_output_tokens": self.max_output_tokens,
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
    target = Path(path) if path else _default_config_path(env)
    file_data = _read_config_file(target)

    config = AppConfig()
    config.raw = file_data

    # Every key the file may set. Anything else is a typo, and a typo that is
    # silently ignored is the worst kind: the file says one thing and the bot
    # does another. Unknown keys are collected so `--check` can name them.
    scalar_keys = (
        "bot_name", "bot_username", "poll_timeout", "debounce_seconds",
        "max_turn_messages", "history_messages", "proactive_min_interval_seconds",
        "proactive_max_per_hour", "proactive_min_confidence", "workspace",
        "db_path", "provider", "base_url", "model", "context_limit",
        "request_timeout", "soul_path", "introduction_path", "system_prompt_path",
        "log_level", "max_output_tokens", "log_summary_chars", "log_status_seconds",
        # Autonomous scheduling and its resource limits. These are the knobs
        # an operator actually needs to tune cost and disturbance, so they
        # must be readable from the file rather than only from Python.
        "idle_seconds", "max_batch_seconds", "max_batch_observations",
        "max_activations_per_hour", "max_actions_per_hour",
        "min_action_interval_seconds",
    )
    bool_keys = ("dry_run", "proactive_enabled")
    list_keys = ("allowed_chat_ids", "admin_user_ids")

    for key in scalar_keys:
        if key in file_data and file_data[key] not in (None, ""):
            setattr(config, key, type(getattr(config, key))(file_data[key]))

    for key in bool_keys:
        if key in file_data:
            setattr(config, key, bool(file_data[key]))

    for key in list_keys:
        if key in file_data and isinstance(file_data[key], list):
            values = []
            for item in file_data[key]:
                try:
                    values.append(int(item))
                except (TypeError, ValueError):
                    continue
            setattr(config, key, tuple(values))

    config.unknown_keys = tuple(
        sorted(set(file_data) - set(scalar_keys) - set(bool_keys) - set(list_keys))
    )

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
    raw_output_cap = env.get("NERVIPULSA_MAX_OUTPUT_TOKENS")
    if raw_output_cap and raw_output_cap.strip():
        try:
            config.max_output_tokens = int(raw_output_cap)
        except ValueError:
            pass
    config.request_timeout = _env_float("NERVIPULSA_TIMEOUT", config.request_timeout)

    root = Path(config.workspace) if config.workspace else _default_workspace(env)
    config.workspace = str(root)
    if not config.db_path:
        config.db_path = str(root / "bot.sqlite3")

    # Identity files. Defaults live inside the package so a fresh checkout runs;
    # an operator overrides them via the config file or the environment.
    prompts = Path(__file__).resolve().parent / "prompts"
    config.soul_path = env.get("BOT_SOUL_PATH", config.soul_path) or str(prompts / "soul.md")
    config.introduction_path = (
        env.get("BOT_INTRODUCTION_PATH", config.introduction_path)
        or str(prompts / "introduction.md")
    )
    # Backwards compatibility: the old single-file setting still works, and an
    # existing deployment keeps running without editing anything. If it is set,
    # it replaces both halves — the operator asked for one specific file.
    legacy = env.get("BOT_SYSTEM_PROMPT_PATH", config.system_prompt_path)
    if legacy:
        config.soul_path = legacy
        config.introduction_path = ""
    return config


def _default_config_path(env: dict[str, str] | os._Environ) -> Path:
    override = env.get("BOT_CONFIG")
    if override:
        return Path(override)
    # Prefer TOML; fall back to the JSON name this project used first. Both are
    # tried by existence, so an existing deployment keeps working untouched.
    for name in ("config.toml", "config.local.toml", "config.local.json"):
        candidate = Path(name)
        if candidate.exists():
            return candidate
    return Path("config.toml")


def _read_config_file(target: Path) -> dict[str, Any]:
    """Load a TOML or JSON config, flattened to `key -> value`.

    Errors are swallowed on purpose: a malformed file must not crash `--check`,
    which is the command you run *because* you suspect the config is wrong. The
    resulting empty config then shows up as "missing" fields in the report.
    """
    if not target.exists():
        return {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}

    if target.suffix.lower() == ".toml":
        try:
            loaded = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {}
        return _flatten(loaded)

    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return _flatten(loaded) if isinstance(loaded, dict) else {}


# TOML sections exist to make the file readable, not to change meaning. The one
# exception is `[bot]`, where `name`/`username` are the natural words; they are
# mapped explicitly rather than by prefixing every section, so a typo cannot
# quietly become a new setting name.
_SECTION_ALIASES: dict[str, dict[str, str]] = {
    "bot": {"name": "bot_name", "username": "bot_username"},
    "proactive": {
        "enabled": "proactive_enabled",
        "min_interval_seconds": "proactive_min_interval_seconds",
        "max_per_hour": "proactive_max_per_hour",
        "min_confidence": "proactive_min_confidence",
    },
    "logging": {"level": "log_level"},
    "behaviour": {
        # `[behaviour] max_turn_messages` already matches; these are the two
        # that a reader would naturally shorten.
        "max_messages": "max_turn_messages",
    },
}


def _flatten(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten one level of TOML sections into the flat key namespace.

    `[telegram].poll_timeout` and a top-level `poll_timeout` are the same
    setting. Values nested deeper than one section are kept as dicts: no
    recognised key is nested, so a nested value is a mistake worth being able
    to see in `raw` rather than silently dropping.
    """
    flat: dict[str, Any] = {}
    for section, value in data.items():
        if not isinstance(value, dict):
            flat[section] = value
            continue
        aliases = _SECTION_ALIASES.get(section, {})
        for key, inner in value.items():
            if isinstance(inner, dict):
                flat[key] = inner
            else:
                flat[aliases.get(key, key)] = inner
    return flat


def _default_workspace(env: dict[str, str] | os._Environ) -> Path:
    override = env.get("BOT_WORKSPACE")
    if override:
        return Path(override)
    base = env.get("LOCALAPPDATA") or env.get("XDG_STATE_HOME") or str(Path.home())
    return Path(base) / "nervipulsa-telegram-bot"


def load_agent_profile(
    soul_path: str | Path,
    introduction_path: str | Path | None = None,
) -> str:
    """Compose the agent profile from its two files.

    A missing or empty file is a startup error, never a silent default: shipping
    a fallback prompt would mean the bot runs with an identity nobody reviewed,
    which is exactly the failure these files exist to prevent.

    Order matters. Soul goes first because it carries the rules that must
    survive anything the introduction says — an operator editing the tone
    section cannot accidentally drop the impersonation ban, and a reviewer
    reading the composed prompt sees the limits before the style.
    """
    parts: list[str] = []

    soul = Path(soul_path)
    soul_text = soul.read_text(encoding="utf-8")
    if not soul_text.strip():
        raise ValueError(f"soul file is empty: {soul}")
    parts.append(soul_text.strip())

    if introduction_path:
        intro = Path(introduction_path)
        intro_text = intro.read_text(encoding="utf-8")
        if not intro_text.strip():
            raise ValueError(f"introduction file is empty: {intro}")
        parts.append(intro_text.strip())

    return "\n\n".join(parts)


def load_system_prompt(path: str | Path) -> str:
    """Read one profile file, verbatim.

    Kept for the single-file path and for callers that only need the raw text.
    `load_agent_profile` is what builds the prompt the model actually sees.
    """
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"system prompt file is empty: {target}")
    return text


def default_profile_paths() -> tuple[Path, Path]:
    """The profile files shipped inside the package."""
    prompts = Path(__file__).resolve().parent / "prompts"
    return prompts / "soul.md", prompts / "introduction.md"


def load_default_profile() -> str:
    """The shipped profile, composed. Used by tests and experiments."""
    soul, introduction = default_profile_paths()
    return load_agent_profile(soul, introduction)


def load_profile(config: AppConfig) -> str:
    """The composed profile for a loaded config, handling the legacy setting."""
    if config.system_prompt_path and not config.introduction_path:
        return load_system_prompt(config.system_prompt_path)
    return load_agent_profile(config.soul_path, config.introduction_path or None)
