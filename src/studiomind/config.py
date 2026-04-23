"""Persistent user configuration for StudioMind (API keys, preferences).

Config lives at a platform-appropriate path:
  Windows: %APPDATA%\\studiomind\\config.json
  macOS:   ~/Library/Application Support/studiomind/config.json
  Linux:   $XDG_CONFIG_HOME/studiomind/config.json (or ~/.config/studiomind/config.json)

Environment variables always take precedence over the config file so power users
can override without editing the file, and CI / tests don't leak into dev configs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def get_config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "studiomind"


def get_config_path() -> Path:
    return get_config_dir() / "config.json"


def load_config() -> dict:
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def get_anthropic_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    return load_config().get("anthropic_api_key")


def set_anthropic_key(key: str) -> None:
    cfg = load_config()
    cfg["anthropic_api_key"] = key.strip()
    save_config(cfg)


def clear_anthropic_key() -> None:
    cfg = load_config()
    cfg.pop("anthropic_api_key", None)
    save_config(cfg)


def key_source() -> str:
    """Returns 'env', 'config', or 'none'."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "env"
    if load_config().get("anthropic_api_key"):
        return "config"
    return "none"


def key_preview(key: str | None = None) -> str | None:
    key = key or get_anthropic_key()
    if not key:
        return None
    if len(key) <= 12:
        return key[:4] + "..."
    return key[:8] + "..." + key[-4:]
