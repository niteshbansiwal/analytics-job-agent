"""Cold-start engine initialisation must be safe under concurrent requests."""
import threading
import time

import pytest
from sqlalchemy import select

from app import db


@pytest.fixture
def cold_engine(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'cold.db'))
    monkeypatch.setattr(db, '_engine', None)
    monkeypatch.setattr(db, '_Session', None)
    yield
    if db._engine is not None:
        db._engine.dispose()


def test_concurrent_first_sessions_do_not_see_half_initialised_engine(cold_engine, monkeypatch):
    original = db.Base.metadata.create_all

    def slow_create_all(bind, *args, **kwargs):
        time.sleep(0.3)  # simulate the network round trip to a remote Postgres
        return original(bind, *args, **kwargs)

    monkeypatch.setattr(db.Base.metadata, 'create_all', slow_create_all)
    errors, start = [], threading.Barrier(8)

    def worker():
        start.wait()
        try:
            with db.session() as s:
                s.execute(select(db.KV)).all()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_failed_setup_retries_instead_of_staying_broken(cold_engine, monkeypatch):
    original = db.Base.metadata.create_all
    calls = {'n': 0}

    def flaky_create_all(bind, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('database temporarily unreachable')
        return original(bind, *args, **kwargs)

    monkeypatch.setattr(db.Base.metadata, 'create_all', flaky_create_all)
    with pytest.raises(RuntimeError):
        with db.session():
            pass
    assert db._engine is None and db._Session is None
    with db.session() as s:
        db.set_kv(s, 'probe', 'ok')
    with db.session() as s:
        assert db.get_kv(s, 'probe') == 'ok'
