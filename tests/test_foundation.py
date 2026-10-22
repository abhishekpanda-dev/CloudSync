import pytest
from streamlit.testing.v1 import AppTest
from pathlib import Path
from storage.base import StorageBackend


def test_storage_contract_cannot_be_instantiated():
    with pytest.raises(TypeError):
        StorageBackend()


def test_welcome_page(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDSYNC_DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    assert not app.exception
    assert app.title[0].value == "CloudSync"
    assert "Full-copy" in app.subheader[0].value
