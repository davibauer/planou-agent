#!/usr/bin/env python3
"""Builds a targeted CV from the user's REAL DOCX files (their CVs in DOCX),
keeping the formatting and the texts they wrote themselves: copies blocks (header, Education, each experience,
Publications, Certifications, Languages) in the requested order and swaps only the Summary (and optionally a Skills line).
Never invents experience: only reorders/omits existing blocks.

  cv_docx.py plan.json "Your Name, Resume, YYYY-MM (Company).docx"   # YYYY-MM = TODAY's year-month, not the base CV's year
plan: {"base": "<docx with the skeleton>", "sources": {"HAC": "<docx>", "BRV": "<docx>"},
        "summary": ["para 1", "para 2", ...], "skills": "optional line",
        "experiences": [["A", "Role at Company 1"], ["B", "Role at Company 2"], ...],
        "languages": ["Portuguese (Native)", "English (Professional)", "Spanish (Conversation)"] (optional),
        "new_entries": {"<key>": {...}} (optional: an experience that does not exist in the DOCX files; goes into "experiences" as ["NOVA", "<key>"],
                                   with the formatting copied from a real block; see new_block),
        "dates": {...}, "cuts": {...} (optional; see adjust_dates, trim_block),
        "intros": {"<experience title>": "sentence"} (optional; see add_intro) }
Old Portuguese plan keys (fontes, experiencias, novas, ...) are still accepted on the command line (renamed by schema.py).
An experience block = from the paragraph whose text starts with the given title up to the next experience title (bold, no
indent, with ' at ' or 'Founder') or up to the 'Publications' heading.
"""
import sys, json, copy
import docx
from docx.shared import Pt

def paras(d): return list(d.paragraphs)

def is_experience_title(p):
    t = p.text.strip()
    return bool(t) and bool(p.runs) and bool(p.runs[0].bold) and p.paragraph_format.left_indent is None \
        and (' at ' in t or t.startswith('Founder')) and not (p.runs[0].font.size and p.runs[0].font.size.pt >= 14)

def is_heading(p):
    return bool(p.text.strip()) and bool(p.runs) and p.runs[0].font.size is not None and p.runs[0].font.size.pt >= 14

def block(d, start_pred, end_pred):
    ps = paras(d); i = next(k for k, p in enumerate(ps) if start_pred(p)); j = i + 1
    while j < len(ps) and not end_pred(ps[j]): j += 1
    return [copy.deepcopy(p._p) for p in ps[i:j]]

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
HLINK = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink'

def remap_links(elements, source, target):
    """When copying blocks from ANOTHER docx, the w:hyperlink r:id keeps pointing at the target's .rels,
    where the same id is another URL (real bug: 'GitHub Repository' became a YouTube live link on 22/09/2026).
    Resolves the link target in the source document and creates/reuses the relationship in the target."""
    if source is target: return elements
    cache = {}
    for el in elements:
        for h in el.iter(W + 'hyperlink'):
            rid = h.get(REL + 'id')
            if not rid: continue
            rel = source.part.rels.get(rid)
            if rel is None: h.attrib.pop(REL + 'id', None); continue
            target_ref = rel.target_ref
            if target_ref not in cache:
                cache[target_ref] = target.part.relate_to(target_ref, HLINK, is_external=True)
            h.set(REL + 'id', cache[target_ref])
    return elements

def experience_block(d, title):
    """title may end in '#N' to take the N-th occurrence (same role at the same company in
    two periods: 'Role at Company#2' takes the second)."""
    n = 1
    if '#' in title: title, n = title.rsplit('#', 1); n = int(n)
    count = [0]
    def is_start(p):
        if p.text.strip().startswith(title) and is_experience_title(p):
            count[0] += 1; return count[0] == n
        return False
    return block(d, is_start, lambda p: is_experience_title(p) or is_heading(p))

def heading_block(d, name):
    return block(d, lambda p: p.text.strip() == name and is_heading(p), is_heading)

def clone_para(template_p, text, bold=None):
    """Copies a template paragraph (formatting) and swaps the text (a single run)."""
    e = copy.deepcopy(template_p._p)
    for r in e.findall('.//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r')[1:]: r.getparent().remove(r)
    from docx.text.paragraph import Paragraph
    p = Paragraph(e, template_p._parent)
    if p.runs: p.runs[0].text = text; (bold is not None) and setattr(p.runs[0], 'bold', bold)
    else: r = p.add_run(text); r.font.name = 'Arial'
    return e

def _set_texts(el, texts):
    """Swaps the text of the runs of a copied paragraph, run by run (keeps each one's bold/font); extra runs are removed."""
    rs = el.findall('.//' + W + 'r')
    for k, r in enumerate(rs):
        if k >= len(texts): r.getparent().remove(r); continue
        ts = r.findall(W + 't')
        for t in ts[1:]: t.getparent().remove(t)
        if ts: ts[0].text = texts[k]; ts[0].set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    return el

def new_block(d, template_title, new):
    """Experience that does not exist in any DOCX (e.g. a current job the base CV does not have yet), built with the
    SAME formatting as a real block (template_title: 'Role at Company' of a DOCX block that has sections, bullets and the
    'Technologies' line; comes from new["template"] in the CV profile).
    new = {"title": "Role at Company", "work_mode": "Remote", "date": "March 2024 - Current", "location": "City, Country",
           "sections": [{"name": "...", "items": ["...", ...]}], "technologies": "..."}. Only facts confirmed by the user."""
    from docx.text.paragraph import Paragraph
    b = experience_block(d, template_title)
    P = [Paragraph(e, d) for e in b]
    t_section = next(e for e, p in zip(b, P) if p.paragraph_format.left_indent == 457200 and p.text.strip())
    t_item = next(e for e, p in zip(b, P) if p.paragraph_format.left_indent == 914400 and 'numPr' in e.xml)
    t_tech = next(e for e, p in zip(b, P) if p.text.strip().startswith('Technologies'))
    empty = next(e for e, p in zip(b, P) if not p.text.strip())
    out = [_set_texts(copy.deepcopy(b[0]), [new['title'], ' ', '| ' + new.get('work_mode', 'Remote')]),
           _set_texts(copy.deepcopy(b[1]), [new['date'] + ' ', '| ' + new.get('location', '')]), copy.deepcopy(empty)]
    for k, sec in enumerate(new.get('sections') or []):
        if sec.get('name'): out.append(_set_texts(copy.deepcopy(t_section), [sec['name']]))
        out += [_set_texts(copy.deepcopy(t_item), [i]) for i in sec['items']]
        out.append(copy.deepcopy(empty))
    if new.get('technologies'): out.append(_set_texts(copy.deepcopy(t_tech), ['Technologies and tools: ', new['technologies']]))
    return out

def trim_block(block, cut):
    """Shortened version of a real block, without rewriting text (the links stay): cut = {"title": "new title" (optional),
    "work_mode": "..." (optional), "cut_from": "start of the paragraph where the cut begins"} -> everything from that
    paragraph to the end of the block goes. E.g.: keep only the livestreams of an experience and drop the rest."""
    def txt(el): return ''.join(t.text or '' for t in el.iter(W + 't')).strip()
    out = list(block)
    mode = cut.get('work_mode')
    if cut.get('cut_from'):
        k = next((i for i, el in enumerate(out) if i > 1 and txt(el).startswith(cut['cut_from'])), None)
        if k is not None: out = out[:k]
    if cut.get('title') or mode:
        rs = out[0].findall('.//' + W + 'r')
        current = [''.join(t.text or '' for t in r.iter(W + 't')) for r in rs]
        new_texts = [cut.get('title', current[0].strip())] + [' '] * max(0, len(rs) - 2) + ['| ' + (mode or 'Remote')]
        _set_texts(out[0], new_texts[:len(rs)])
    return out

def add_intro(block, text):
    """plan['intros'][title]: a sentence put at the start of the first text paragraph after the title and date lines
    (e.g. "Personal side project, built alongside my client work."), with that paragraph's own formatting."""
    body = [el for el in block[2:] if ''.join(t.text or '' for t in el.iter(W + 't')).strip()]
    if not body: return block
    t = next(body[0].iter(W + 't'))
    t.text = text.rstrip() + ' ' + (t.text or ''); t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    return block

def adjust_dates(doc, dates):
    """plan['dates'] = {<start of the experience title>: <new date line>} — "wildcard" experience: shorten the end
    so it does not overlap the next experience."""
    ps = paras(doc)
    for i, p in enumerate(ps):
        for title, new in dates.items():
            if p.text.strip().startswith(title) and is_experience_title(p):
                for q in ps[i+1:i+4]:
                    if q.text.strip():
                        if q.runs: q.runs[0].text = new
                        for r in q.runs[1:]: r.text = ''
                        break
                break

def build(plan, out_path):
    base = docx.Document(plan['base'])
    sources = {k: docx.Document(v) for k, v in plan['sources'].items()}
    ps = paras(base)
    # formatting templates: a summary text paragraph and an empty one
    i_sum = next(k for k, p in enumerate(ps) if p.text.strip() == 'Summary')
    template_text = next(p for p in ps[i_sum + 1:] if p.text.strip())
    template_empty = next(p for p in ps[i_sum + 1:] if not p.text.strip())
    header = [copy.deepcopy(p._p) for p in ps[:i_sum]]                    # name, location, linkedin, email + spacing
    h_sum = [copy.deepcopy(ps[i_sum]._p)]
    new_summary = []
    for t in plan['summary']:
        new_summary += [copy.deepcopy(template_empty._p), clone_para(template_text, t)]
    if plan.get('skills'):
        new_summary += [copy.deepcopy(template_empty._p), clone_para(template_text, 'Skills: ' + plan['skills'])]
    new_summary += [copy.deepcopy(template_empty._p), copy.deepcopy(template_empty._p)]
    edu = heading_block(base, 'Education')
    h_exp = [copy.deepcopy(next(p for p in ps if p.text.strip() == 'Experience' and is_heading(p))._p), copy.deepcopy(template_empty._p)]
    exps = []
    def empty(el): return not ''.join(t.text or '' for t in el.iter(W + 't')).strip()
    for n, (src, title) in enumerate(plan['experiences']):
        if src == 'NOVA':             # experience that is in no DOCX: plan['new_entries'][title], format copied from a real block
            new = plan['new_entries'][title]
            if not new.get('template'): sys.exit(f'experiencia nova "{title}": falta "modelo" (titulo de um bloco real com secoes e bullets para copiar a formatacao)')
            block = new_block(sources[new.get('template_source', next(iter(sources)))], new['template'], new)
        else:
            block = remap_links(experience_block(sources[src], title), sources[src], base)
            cut = (plan.get('cuts') or {}).get(title)
            if cut: block = trim_block(block, cut)
        intro = (plan.get('intros') or {}).get(title)
        if intro: block = add_intro(block, intro)
        # each source CV ends the block with 0, 1 or 2 empty lines; without normalizing, one experience
        # sticks to the next. CV standard: 1 blank line between
        # experiences and 2 before the next section title.
        while block and empty(block[-1]): block.pop()
        extra = 2 if n == len(plan['experiences']) - 1 else 1
        exps += block + [copy.deepcopy(template_empty._p) for _ in range(extra)]
    pub = heading_block(base, 'Publications'); cert = heading_block(base, 'Certifications')
    lang = heading_block(base, 'Languages')
    if plan.get('languages'):
        h = lang[0]; template_lang = next(p for p in ps if p.text.strip() == 'Portuguese (Native)')
        lang = [h, copy.deepcopy(template_empty._p)] + [clone_para(template_lang, t) for t in plan['languages']]
    # rewrites the body
    body = base.element.body
    for el in list(body):
        if el.tag.endswith('}p') or el.tag.endswith('}tbl'): body.remove(el)
    sect = body.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}sectPr')
    for el in header + h_sum + new_summary + edu + h_exp + exps + pub + cert + lang:
        (sect.addprevious(el) if sect is not None else body.append(el))
    if plan.get('dates'): adjust_dates(base, plan['dates'])
    base.save(out_path)

if __name__ == '__main__':
    import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import schema
    plan = json.load(open(sys.argv[1]))
    plan = schema.config({'cv_profiles': {'plan': plan}}, force=True)['cv_profiles']['plan']    # old Portuguese plan keys still work
    build(plan, sys.argv[2]); print('ok', sys.argv[2])
