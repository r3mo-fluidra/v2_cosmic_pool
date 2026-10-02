"""
Smoke test: does app.py — the actual Streamlit entrypoint — boot?

Uses Streamlit's own headless test runner (`streamlit.testing.v1.AppTest`)
to execute app.py top to bottom the way Streamlit Cloud / a container would,
without a browser or a live server. No external credentials are required:
every network client in this codebase (Neo4j driver, Gemini client, Qdrant
client, AWS Memory client) is constructed lazily inside a function, so
loading the script only exercises imports and the initial render — exactly
the class of failure (a missing dependency, a broken import, a renamed
symbol) that should fail CI instead of a production deploy.

This does not drive a conversation turn: that requires real model/DB access
and belongs to manual verification (scripts/smoke_memory_*.py, t_vision.py),
not an automatic, credential-free CI run.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[2] / "app.py"


@pytest.fixture
def no_creds_env(monkeypatch):
    """Dummy values so any accidental eager read of these doesn't KeyError.

    Scoped to this test only — other tests must not start depending on these
    being set.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setenv("NEO4J_URI", "neo4j://dummy")
    monkeypatch.setenv("NEO4J_USER", "dummy")
    monkeypatch.setenv("NEO4J_PASSWORD", "dummy")


def test_app_boots_without_exceptions(no_creds_env):
    at = AppTest.from_file(str(APP_PATH))
    at.run(timeout=30)
    assert not at.exception, [str(e) for e in at.exception]
