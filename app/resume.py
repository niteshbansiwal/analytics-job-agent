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

def tailored_pdf(jd: str, title: str = '') -> bytes:
    """Conservative tailoring: factual bullets unchanged; reorder skills and bullets by JD relevance."""
    info = resume_facts()
    terms = keywords(jd + ' ' + title)
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
    for position in info['experience']:
        title_line = (f"{escape(position['company'])} | {escape(position['title'])}"
                      f" <font color='#68788B'>({escape(position['dates'])})</font>")
        ordered = sorted(position['bullets'], key=lambda b: -len(set(b.lower().replace('-', ' ').split()) & terms))
        group = [Paragraph(title_line, heading2)]
        for entry in ordered:
            group.append(Paragraph(f"&#8226; {escape(entry)}", bullet))
        # Allow the job section to span pages; title stays with the first point.
        story.extend(group)
    story += [Paragraph('EDUCATION', heading), Paragraph(escape(info['education']), small)]
    doc.build(story)
    return output.getvalue()
