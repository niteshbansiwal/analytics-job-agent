from __future__ import annotations
from io import BytesIO
from xml.sax.saxutils import escape
import yaml
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from .config import ROOT


def resume_facts():
    return yaml.safe_load((ROOT / 'config' / 'resume_facts.yaml').read_text(encoding='utf-8'))

def keywords(jd: str) -> set[str]:
    return {word.lower() for word in jd.replace('-', ' ').split() if len(word) > 3}

def _bullet_score(bullet: str, terms: set[str]) -> int:
    words = {w.lower().strip('.,;:()') for w in bullet.replace('-', ' ').split()}
    return len(words & terms)


def resume_selection(jd: str, title: str = '', selected_fact_ids: list[str] | None = None) -> dict:
    """Selectable, immutable source-resume bullets. No AI-written resume claims."""
    info = resume_facts()
    terms = keywords(jd + ' ' + title)
    fact_links = {
        'amex_sme': (0, 4), 'amex_genai': (0, 0),
        'amex_call_compliance': (0, 1), 'amex_nl_sql': (0, 2),
        'plivo_margin': (1, 4), 'plivo_fraud': (1, 2),
        'dashboards': (1, 6), 'stakeholders': (0, 6),
        'zs_forecasting': (2, 1),
    }
    boosted = {fact_links[x] for x in (selected_fact_ids or []) if x in fact_links}
    defaults = (4, 4, 2, 1)
    positions = []
    for pidx, position in enumerate(info['experience']):
        scores = sorted(range(len(position['bullets'])),
                        key=lambda i: (-(100 if (pidx, i) in boosted else 0) -
                                       _bullet_score(position['bullets'][i], terms), i))
        selected = set(scores[:defaults[pidx] if pidx < len(defaults) else 1])
        positions.append({
            'index': pidx, 'company': position['company'],
            'title': position['title'], 'dates': position['dates'],
            'bullets': [{'index': i, 'text': b, 'selected': i in selected}
                        for i, b in enumerate(position['bullets'])],
        })
    return {
        'source': 'Nitesh_Bansiwal_Resume_-_2026-08(1).pdf',
        'headline': info['headline'],
        'summary': info['summary'],
        'positions': positions,
        'all_claims_from_master_resume': True,
    }


def validate_bullet_selection(selection: dict | None, info: dict) -> dict[int, list[int]] | None:
    """Strict validation: user may select/remove source facts but cannot inject text."""
    if selection is None:
        return None
    if not isinstance(selection, dict):
        raise ValueError('Resume bullet selection must be an object')
    expected = {str(i) for i in range(len(info['experience']))}
    if set(selection) != expected:
        raise ValueError('Select resume bullets for every experience section')
    chosen = {}
    for key, indices in selection.items():
        role_index = int(key)
        count = len(info['experience'][role_index]['bullets'])
        if (not isinstance(indices, list) or not indices or len(indices) > count
                or any(type(i) is not int or i < 0 or i >= count for i in indices)
                or len(set(indices)) != len(indices)):
            raise ValueError('Resume bullet selection contains invalid or duplicate indices')
        chosen[role_index] = sorted(indices)
    return chosen


def tailored_pdf(jd: str, title: str = '', *, selected_bullets: dict | None = None) -> bytes:
    """Conservative tailoring: factual bullets unchanged; reorder skills and bullets by JD relevance."""
    info = resume_facts()
    terms = keywords(jd + ' ' + title)
    chosen = validate_bullet_selection(selected_bullets, info)
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=17*mm, leftMargin=17*mm,
                            topMargin=13*mm, bottomMargin=12*mm, title=f"{info['name']} | Resume")
    navy = colors.HexColor('#16243D')
    small = ParagraphStyle('small', fontName='Helvetica', fontSize=8.1, leading=10.6, spaceAfter=2)
    bullet = ParagraphStyle('bullet', parent=small, leftIndent=9, firstLineIndent=-7, spaceAfter=1.8)
    heading = ParagraphStyle('heading', fontName='Helvetica-Bold', fontSize=9.0, leading=11.8,
                             textColor=navy, spaceBefore=6, spaceAfter=3)
    heading2 = ParagraphStyle('job', fontName='Helvetica-Bold', fontSize=8.5, leading=11, spaceBefore=5)
    name = ParagraphStyle('name', fontName='Helvetica-Bold', fontSize=18, leading=21, alignment=TA_CENTER, textColor=navy)
    center = ParagraphStyle('center', parent=small, alignment=TA_CENTER, fontSize=8.0)
    story = [Paragraph(escape(info['name']).upper(), name),
             Paragraph(escape(info['headline']), center),
             Paragraph(escape(f"{info['contact']['email']} | {info['contact']['phone']} | {info['contact']['linkedin']}"), center),
             Spacer(1, 6), Paragraph('PROFILE', heading), Paragraph(escape(info['summary']), small),
             Paragraph('TECHNICAL SKILLS', heading)]
    categories = list(info['skills'].items())
    # Selectively emphasize relevant capabilities without introducing new ones.
    categories.sort(key=lambda item: -sum(len(set(s.lower().split()) & terms) for s in item[1]))
    for label, skills in categories:
        ranked = sorted(skills, key=lambda x: -len(set(x.lower().split()) & terms))
        story.append(Paragraph(f"<b>{escape(label)}:</b> {escape(' | '.join(ranked))}", small))
    story.append(Paragraph('PROFESSIONAL EXPERIENCE', heading))
    for pidx, position in enumerate(info['experience']):
        title_line = (f"{escape(position['company'])} | {escape(position['title'])}"
                      f" <font color='#68788B'>({escape(position['dates'])})</font>")
        if chosen is None:
            ordered = sorted(position['bullets'], key=lambda b: -_bullet_score(b, terms))
        else:
            ordered = [position['bullets'][i] for i in chosen[pidx]]
        group = [Paragraph(title_line, heading2)]
        for entry in ordered:
            group.append(Paragraph(f"&#8226; {escape(entry)}", bullet))
        # Allow the job section to span pages; title stays with the first point.
        story.extend(group)
    story += [Paragraph('EDUCATION', heading), Paragraph(escape(info['education']), small)]
    doc.build(story)
    return output.getvalue()
