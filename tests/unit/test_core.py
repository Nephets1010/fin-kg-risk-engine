"""Unit tests for the configuration, logging, error taxonomy and CLI."""

from __future__ import annotations

import json

import pytest

from fin_kg.config.settings import Settings, get_settings, reset_settings_cache
from fin_kg.core.errors import (
    AuthenticationError,
    FinKgError,
    GraphStoreError,
    NotFoundError,
    ValidationError,
)
from fin_kg.core.logging import (
    JsonFormatter,
    configure_logging,
    get_logger,
    mask_secrets,
    register_secret_filter,
)


class TestSettings:
    def test_defaults(self) -> None:
        settings = Settings()
        assert settings.env == "test"
        assert settings.neo4j.uri.startswith("bolt://")

    def test_log_level_is_upper_cased(self) -> None:
        assert Settings(log_level="debug").log_level == "DEBUG"

    def test_invalid_log_level_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Settings(log_level="verbose")

    def test_critical_threshold_must_exceed_alert_threshold(self) -> None:
        with pytest.raises(ValueError):
            Settings(risk={"alert_threshold": 90.0, "critical_threshold": 50.0})

    def test_production_requires_neo4j_password(self) -> None:
        with pytest.raises(ValueError):
            Settings(env="production", neo4j={"password": ""})

    def test_production_accepts_a_password(self) -> None:
        settings = Settings(env="production", neo4j={"password": "s3cret-value"}, api_key="k")
        assert settings.neo4j.password.get_secret_value() == "s3cret-value"

    @pytest.mark.parametrize(
        "password", ["fin-kg-dev-password", "neo4j", "password", "changeme", "secret"]
    )
    def test_production_refuses_the_development_defaults(self, password: str) -> None:
        """The compose file ships a default password; production must not accept it.

        The deployment checklist says the password must be "set and not the
        default value". Before this rule existed only the empty password was
        rejected, so a production deployment that forgot to override the
        compose default started happily with a known password.
        """
        with pytest.raises(ValueError):
            Settings(env="production", neo4j={"password": password})

    @pytest.mark.parametrize("env", ["development", "test", "staging"])
    def test_non_production_accepts_the_development_defaults(self, env: str) -> None:
        settings = Settings(env=env, neo4j={"password": "fin-kg-dev-password"})
        assert settings.neo4j.password.get_secret_value() == "fin-kg-dev-password"

    def test_llm_enabled_requires_api_key_in_production(self) -> None:
        with pytest.raises(ValueError):
            Settings(
                env="production",
                neo4j={"password": "s3cret-value"},
                llm={"enabled": True, "api_key": ""},
            )

    def test_cors_origins_accept_a_csv_string(self) -> None:
        settings = Settings(cors_origins="http://a.test,http://b.test")
        assert settings.cors_origins == ["http://a.test", "http://b.test"]

    def test_resolve_path_is_absolute(self) -> None:
        settings = Settings()
        assert settings.resolve_path("rules/risk_rules.yaml").is_absolute()

    def test_settings_are_cached(self) -> None:
        assert get_settings() is get_settings()

    def test_cache_can_be_reset(self) -> None:
        first = get_settings()
        reset_settings_cache()
        assert get_settings() is not first


class TestErrors:
    def test_error_carries_code_and_status(self) -> None:
        error = NotFoundError("missing", details={"id": "x"})
        assert error.code == "not_found"
        assert error.http_status == 404
        assert error.to_dict()["details"] == {"id": "x"}

    def test_validation_error_is_not_retryable(self) -> None:
        assert ValidationError("bad").retryable is False

    def test_dependency_error_is_retryable(self) -> None:
        assert GraphStoreError("down").retryable is True

    def test_all_errors_derive_from_the_base(self) -> None:
        assert issubclass(AuthenticationError, FinKgError)


class TestLogging:
    def test_json_formatter_emits_json(self) -> None:
        import logging

        record = logging.LogRecord(
            name="fin_kg.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"

    def test_secrets_are_masked(self) -> None:
        register_secret_filter("super-secret-token")
        assert "super-secret-token" not in mask_secrets("token=super-secret-token")

    def test_short_values_are_not_registered(self) -> None:
        register_secret_filter("ab")
        assert mask_secrets("ab") == "ab"

    def test_configure_logging_is_idempotent(self) -> None:
        configure_logging("INFO", "json")
        configure_logging("INFO", "json")
        logger = get_logger()
        assert logger.handlers
        assert len(logger.handlers) == 1


class TestCLI:
    def test_parser_accepts_known_commands(self) -> None:
        from fin_kg.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["seed", "--companies", "5"])
        assert args.command == "seed"
        assert args.companies == 5

    def test_unknown_command_exits(self) -> None:
        from fin_kg.cli import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["nope"])

    def test_schema_command_runs(self, capsys: pytest.CaptureFixture[str]) -> None:
        from fin_kg.cli import main

        assert main(["schema"]) == 0
        assert "Company" in capsys.readouterr().out
