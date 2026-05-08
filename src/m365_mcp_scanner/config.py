from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


def _default_data_dir() -> Path:
    return Path.home() / ".m365-mcp-scanner"


class TomlConfigSource(PydanticBaseSettingsSource):
    """Loads ~/.m365-mcp-scanner/config.toml if present."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: ARG002
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = _default_data_dir() / "config.toml"
        if not path.is_file():
            return {}
        try:
            with path.open("rb") as f:
                return tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="M365_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tenant_id: str = ""
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    data_dir: Path = Field(default_factory=_default_data_dir)
    concurrency: int = 16
    probe_enabled: bool = False
    activity_window_days: int = 30

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: init (CLI overrides) > env > dotenv > toml > defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSource(settings_cls),
            file_secret_settings,
        )

    def snapshot(self) -> dict[str, object]:
        """Subset of settings safe to embed in a ScanDocument."""
        return {
            "probe_enabled": self.probe_enabled,
            "activity_window_days": self.activity_window_days,
            "concurrency": self.concurrency,
        }
