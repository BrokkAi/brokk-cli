import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

def get_global_config_dir() -> Path:
    """Returns the platform-appropriate global configuration directory for Brokk.
    Mirrors ai.brokk.util.BrokkConfigPaths.java.
    """
    home = Path.home()
    if sys.platform == "win32":
        app_data = os.getenv("APPDATA")
        base = Path(app_data) if app_data else home / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        xdg = os.getenv("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else home / ".config"
    return base / "Brokk"


def get_global_cache_dir() -> Path:
    """Returns the platform-appropriate global cache directory for Brokk."""
    home = Path.home()
    if sys.platform == "win32":
        local_app_data = os.getenv("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        return base / "Brokk" / "Cache"
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "Brokk"
    xdg = os.getenv("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else home / ".cache"
    return base / "Brokk"


def settings_dir() -> Path:
    """Returns the legacy .brokk directory in home for settings.json."""
    return Path.home() / ".brokk"


def settings_file() -> Path:
    return settings_dir() / "settings.json"


@dataclass
class Settings:
    """Persisted CLI preferences."""

    last_model: str | None = None
    last_reasoning_effort: str | None = None
    last_auto_commit: bool | None = None

    @classmethod
    def load(cls) -> "Settings":
        """Loads settings from disk, returning defaults if file is missing or corrupt.

        Unknown legacy keys are ignored so settings files written by older
        versions remain readable.
        """
        settings_path = settings_file()
        if not settings_path.exists():
            return cls()

        try:
            with settings_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                # Only pass known fields to the dataclass to avoid issues if the file
                # contains unexpected keys. Build kwargs from fields present in data.
                # We rely on dataclass defaults for any missing fields.
                valid_keys = {field.name for field in cls.__dataclass_fields__.values()}
                filtered = {k: v for k, v in (data or {}).items() if k in valid_keys}
                return cls(**filtered)
        except Exception as e:
            logger.warning("Failed to load settings from %s: %s. Using defaults.", settings_path, e)
            return cls()

    def save(self) -> None:
        """Saves current settings to disk atomically.

        Raises:
            OSError: If the settings file cannot be written.
        """
        try:
            settings_path = settings_file()
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp_file = settings_path.with_suffix(".tmp")
            with temp_file.open("w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=4)
            temp_file.replace(settings_path)
        except Exception as e:
            logger.error("Failed to save settings to %s: %s", settings_file(), e)
            raise
