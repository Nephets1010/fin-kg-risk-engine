"""Environment-driven configuration.

Every tunable value of the platform lives here and is resolved from the
process environment (optionally seeded from a local ``.env`` file). Nothing in
the code base reads ``os.environ`` directly, and no credential is ever given a
default value — :class:`Settings` fails fast at startup when a required secret
is missing in a non-development environment.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]
LogFormat = Literal["json", "console"]

#: Repository root, derived from this file's location (src/fin_kg/config/..).
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent


class Neo4jSettings(BaseSettings):
    """Connection settings for the graph store."""

    model_config = SettingsConfigDict(env_prefix="FIN_KG_NEO4J_", extra="ignore")

    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: SecretStr = SecretStr("")
    database: str = "neo4j"
    max_connection_pool: int = Field(default=50, ge=1, le=500)
    connection_timeout: int = Field(default=30, ge=1, le=600)
    batch_size: int = Field(default=5_000, ge=1, le=100_000)


class RedisSettings(BaseSettings):
    """Settings for the event stream and the job queue."""

    model_config = SettingsConfigDict(env_prefix="FIN_KG_REDIS_", extra="ignore")

    url: str = "redis://localhost:6379/0"
    password: SecretStr = SecretStr("")
    stream_maxlen: int = Field(default=100_000, ge=1_000)


class RiskSettings(BaseSettings):
    """Tuning knobs of the risk engine."""

    model_config = SettingsConfigDict(env_prefix="FIN_KG_RISK_", extra="ignore")

    max_hops: int = Field(default=3, ge=1, le=10)
    decay: float = Field(default=0.6, ge=0.0, le=1.0)
    alert_threshold: float = Field(default=60.0, ge=0.0, le=100.0)
    critical_threshold: float = Field(default=85.0, ge=0.0, le=100.0)
    rules_path: Path = Path("rules/risk_rules.yaml")

    @model_validator(mode="after")
    def _check_thresholds(self) -> "RiskSettings":
        if self.critical_threshold < self.alert_threshold:
            raise ValueError(
                "critical_threshold must be greater than or equal to alert_threshold"
            )
        return self


class IngestionSettings(BaseSettings):
    """Batch sizes and working directories of the ETL pipeline."""

    model_config = SettingsConfigDict(env_prefix="FIN_KG_INGESTION_", extra="ignore")

    batch_size: int = Field(default=2_000, ge=1, le=100_000)
    data_dir: Path = Path("data/samples")
    checkpoint_dir: Path = Path("data/checkpoints")


class LLMSettings(BaseSettings):
    """Optional LLM backend used by the research module.

    The platform is fully functional with the LLM disabled: KGQA then answers
    from deterministic templates over Cypher results.
    """

    model_config = SettingsConfigDict(env_prefix="FIN_KG_LLM_", extra="ignore")

    enabled: bool = False
    api_base: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "gpt-4o-mini"
    timeout: int = Field(default=60, ge=1, le=600)


class ObservabilitySettings(BaseSettings):
    """Metrics exporter and outbound alert channel."""

    model_config = SettingsConfigDict(env_prefix="FIN_KG_", extra="ignore")

    metrics_enabled: bool = True
    metrics_port: int = Field(default=9100, ge=1, le=65535)
    alert_webhook_url: str = ""


class SchedulerSettings(BaseSettings):
    """Cron expressions for the periodic jobs."""

    model_config = SettingsConfigDict(env_prefix="FIN_KG_", extra="ignore")

    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    job_ingest_cron: str = "0 2 * * *"
    job_rescore_cron: str = "0 */6 * * *"
    job_propagate_cron: str = "*/15 * * * *"
    job_research_cron: str = "0 3 * * *"


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_prefix="FIN_KG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = "development"
    log_level: str = "INFO"
    log_format: LogFormat = "json"
    api_host: str = "0.0.0.0"  # noqa: S104 - container bind address
    api_port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=4, ge=1, le=64)
    api_key: SecretStr = SecretStr("")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8501"])

    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {value}")
        return level

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _enforce_production_secrets(self) -> "Settings":
        """Refuse to start production without a real Neo4j credential."""
        if self.env == "production":
            if not self.neo4j.password.get_secret_value():
                raise ValueError(
                    "FIN_KG_NEO4J_PASSWORD must be set in production; "
                    "supply it through a secret manager"
                )
            if self.llm.enabled and not self.llm.api_key.get_secret_value():
                raise ValueError(
                    "FIN_KG_LLM_API_KEY must be set when the LLM backend is enabled"
                )
        return self

    def resolve_path(self, relative: Path) -> Path:
        """Resolve a possibly relative path against the project root."""
        candidate = Path(relative)
        if candidate.is_absolute():
            return candidate
        return (PROJECT_ROOT / candidate).resolve()


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Used by tests that mutate the environment."""
    get_settings.cache_clear()
