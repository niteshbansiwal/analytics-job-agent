"""One-page original resume invariants; optional real-master fixture via env."""
import os
import fitz
import pytest
from app.resume_master import (validate_selection, validate_master,
                               master_selection, render_master, MIN_PROJECTS)


def all_projects():
    return {str(i): list(range(n)) for i, n in enumerate((7, 7, 3, 1))}


def test_80_percent_core_and_locked_projects():
    assert MIN_PROJECTS == 15
    assert sum(len(v) for v in validate_selection(all_projects()).values()) == 18
    selected = all_projects()
    selected['0'].remove(6)
    with pytest.raises(ValueError, match='Core'):
        validate_selection(selected)
    selected = all_projects()
    selected['1'] = [0, 1, 2]
    with pytest.raises(ValueError, match='80%'):
        validate_selection(selected)
    selected = all_projects()
    selected['0'].remove(2)
    selected['1'].remove(3)
    selected['2'].remove(1)
    assert sum(len(v) for v in validate_selection(selected).values()) == 15
    with pytest.raises(ValueError):
        validate_selection({'0': [0]})
    with pytest.raises(ValueError):
        validate_selection({'0': [0, 0], '1': [0], '2': [0], '3': [0]})


def test_original_master_layout_when_fixture_available():
    path = os.getenv('MASTER_RESUME_PDF', '')
    if not path:
        pytest.skip('Real private master PDF not checked into source control')
    raw = open(path, 'rb').read()
    validate_master(raw)
    with fitz.open(stream=raw, filetype='pdf') as original:
        before = original[0].get_text()
    selected = master_selection(raw)
    assert len(selected['positions']) == 4
    assert sum(len(p['bullets']) for p in selected['positions']) == 18
    assert render_master(raw, all_projects()) == raw
    altered = all_projects()
    altered['0'].remove(2)
    altered['1'].remove(3)
    altered['2'].remove(1)
    result = render_master(raw, altered)
    with fitz.open(stream=result, filetype='pdf') as doc:
        assert len(doc) == 1
        assert doc[0].rect == fitz.Rect(0, 0, 612, 792)
        text = doc[0].get_text()
        for anchor in ('NITESH BANSIWAL', 'PROFESSIONAL SUMMARY', 'TECHNICAL SKILLS',
                       'PROFESSIONAL EXPERIENCE', 'EDUCATION', 'Indian Institute of Technology'):
            assert anchor in text and anchor in before
        assert max(b[3] for b in doc[0].get_text('blocks')) > 710
        assert len(result) > 10_000
