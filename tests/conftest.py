import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    from quizcaster import config, db
    monkeypatch.setattr(config, "SECRET_KEY_PATH", tmp_path / "secret.key")
    monkeypatch.setattr(db, "_fernet", None)
    db.reset_for_tests(tmp_path / "test.db")
    yield
    db.reset_for_tests(tmp_path / "unused.db")
