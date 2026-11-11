"""Runtime configuration for the Larkfield support agent.

All values come from the environment so the deployed service can use its own
model provider key. Nothing secret is committed.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _serverless() -> bool:
    """True inside Vercel/Lambda or when SERVERLESS=1 is set explicitly."""
    return bool(
        os.getenv("VERCEL")
        or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        or os.getenv("SERVERLESS")
    )


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    port: int
    data_dir: Path
    build_dir: Path
    run_db_path: Path
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_input_per_mtok: float
    llm_output_per_mtok: float
    llm_timeout_seconds: float
    llm_judge_enabled: bool
    llm_classifier_enabled: bool
    max_body_chars: int
    serverless: bool

    @classmethod
    def from_env(cls) -> "Config":
        serverless = _serverless()
        temp = Path(tempfile.gettempdir())
        return cls(
            port=_int("PORT", 8000),
            data_dir=Path(_env("DATA_DIR", str(ROOT / "data"))),
            build_dir=Path(
                _env(
                    "DATA_BUILD_DIR",
                    str(temp / "larkfield-build" if serverless else ROOT / "build"),
                )
            ),
            run_db_path=Path(
                _env(
                    "RUN_DB_PATH",
                    str(temp / "larkfield-runs.db" if serverless else ROOT / "var" / "runs.db"),
                )
            ),
            llm_api_key=_env("LLM_API_KEY"),
            llm_base_url=_env("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_model=_env("LLM_MODEL", "gpt-4o-mini"),
            llm_input_per_mtok=_float("LLM_INPUT_USD_PER_MTOK", 0.15),
            llm_output_per_mtok=_float("LLM_OUTPUT_USD_PER_MTOK", 0.60),
            llm_timeout_seconds=_float("LLM_TIMEOUT_SECONDS", 8.0),
            llm_judge_enabled=_bool("LLM_JUDGE_ENABLED", True),
            llm_classifier_enabled=_bool("LLM_CLASSIFIER_ENABLED", True),
            max_body_chars=_int("MAX_BODY_CHARS", 20000),
            serverless=serverless,
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.llm_input_per_mtok
            + output_tokens / 1_000_000 * self.llm_output_per_mtok
        )


def get_config() -> Config:
    return Config.from_env()
