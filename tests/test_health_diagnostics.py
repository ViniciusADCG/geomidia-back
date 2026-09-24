import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.health import preview_database_diagnostics


def test_preview_diagnostics_identify_missing_database_url_without_exposing_error_text():
    error = RuntimeError("connection refused at localhost with secret password")

    diagnostics = preview_database_diagnostics(
        error,
        "postgresql+psycopg://geomidia:secret@localhost:5432/geomidia",
        {},
    )

    assert diagnostics == {
        "database_url_configured": False,
        "database_target": "local",
        "database_port": 5432,
        "connection_issue": "connection_refused",
    }
    assert "secret" not in str(diagnostics)


def test_preview_diagnostics_classify_remote_authentication_error():
    diagnostics = preview_database_diagnostics(
        RuntimeError("password authentication failed for user hidden"),
        "postgresql+psycopg://user:secret@pooler.example.com:6543/postgres",
        {"DATABASE_URL": "configured"},
    )

    assert diagnostics == {
        "database_url_configured": True,
        "database_target": "remote",
        "database_port": 6543,
        "connection_issue": "authentication",
    }
