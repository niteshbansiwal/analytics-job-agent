"""Each test owns an isolated disposable database; never read production state."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    from app import db
    test_engine = create_engine('sqlite:///' + str(tmp_path / 'atlas.db'),
                                connect_args={'check_same_thread': False})
    db.Base.metadata.create_all(test_engine)
    monkeypatch.setenv('DEMO_MODE', 'true')
    monkeypatch.delenv('VERCEL', raising=False)
    monkeypatch.setattr(db, '_engine', test_engine)
    monkeypatch.setattr(db, '_Session', sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False))
    yield
    test_engine.dispose()
