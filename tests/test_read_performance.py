import json
from sqlalchemy import event
from fastapi.testclient import TestClient
from app.db import session,KV
from app.read_model import totals


def capture(db):
    queries=[]
    def record(conn,cursor,statement,parameters,context,executemany):
        queries.append((statement,str(parameters)))
    event.listen(db.bind,'before_cursor_execute',record)
    return queries,record


def test_counters_use_one_round_trip():
    with session() as db:
        queries,record=capture(db)
        try:
            result=totals(db)
            assert result['jobs']==0 and result['sent']==0
            assert len(queries)==1
        finally:event.remove(db.bind,'before_cursor_execute',record)


def test_compact_context_does_not_read_attachment_blobs(monkeypatch):
    from app.main import app,owner
    monkeypatch.setenv('DEMO_MODE','false')
    with session() as db:
        for kind in ('analytics','data_science'):
            db.add(KV(key='fixed_resume_meta:'+kind,value=json.dumps({'id':kind,'label':kind,'sha256':'a'*64})))
            db.add(KV(key='fixed_resume:'+kind,value='Do not read this PDF'))
    app.dependency_overrides[owner]=lambda:'offline-owner'
    try:
        with session() as db:
            queries,record=capture(db)
            try:response=TestClient(app).get('/api/overview?section=outbox')
            finally:event.remove(db.bind,'before_cursor_execute',record)
        assert response.status_code==200
        assert len(queries)<=6
        assert not any('fixed_resume:' in parameters for _,parameters in queries)
        assert 'app;dur=' in response.headers['server-timing']
    finally:app.dependency_overrides.clear()


def test_resume_metadata_changes_with_replacement():
    from app.resume_library import save_resume,list_resumes,digest
    from io import BytesIO
    from reportlab.pdfgen import canvas
    def pdf(text):
        out=BytesIO();doc=canvas.Canvas(out);doc.drawString(30,700,text);doc.save();return out.getvalue()
    first,second=pdf('First'),pdf('Second')
    with session() as db:save_resume(db,'analytics',first)
    with session() as db:
        assert next(r for r in list_resumes(db) if r['id']=='analytics')['sha256']==digest(first)
        save_resume(db,'analytics',second,expected_sha256=digest(first))
    with session() as db:
        assert next(r for r in list_resumes(db) if r['id']=='analytics')['sha256']==digest(second)
