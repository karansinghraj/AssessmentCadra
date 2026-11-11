"""Serverless path defaults must never write inside the repository."""
from __future__ import annotations

import tempfile
from pathlib import Path

from src.config import ROOT, Config


def _clear(monkeypatch):
    for key in (
        "VERCEL",
        "AWS_LAMBDA_FUNCTION_NAME",
        "SERVERLESS",
        "RUN_DB_PATH",
        "DATA_BUILD_DIR",
        "DATA_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


def test_serverless_uses_temp_directory(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("VERCEL", "1")
    cfg = Config.from_env()
    temp = Path(tempfile.gettempdir()).resolve()
    assert cfg.serverless is True
    assert cfg.run_db_path.resolve().parent == temp
    assert cfg.build_dir.resolve().parent == temp
    assert cfg.run_db_path.name == "larkfield-runs.db"
    assert cfg.build_dir.name == "larkfield-build"


def test_local_uses_repository_paths(monkeypatch):
    _clear(monkeypatch)
    cfg = Config.from_env()
    assert cfg.serverless is False
    assert cfg.run_db_path == ROOT / "var" / "runs.db"
    assert cfg.build_dir == ROOT / "build"
