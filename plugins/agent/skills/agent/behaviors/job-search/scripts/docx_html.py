#!/usr/bin/env python3
"""docx (from cv_docx.py) -> simple HTML with inline styles (Arial, sizes, bold, indents) for the Google Drive connector
to convert into a Google Doc (create_file with contentMimeType=text/html)."""
import sys, html, re
import docx

def run_html(r):
    t = html.escape(r.text)
    if not t: return ''
    if r.bold: t = f'<b>{t}</b>'
    if r.italic: t = f'<i>{t}</i>'
    return t

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'

def run_txt(r):
    # w:br (line break inside the paragraph) becomes <br>: without it 'Solutions' stuck to 'Microsoft' in AZ-400
    t = ''.join((x.text or '') if x.tag == W + 't' else '\n' for x in r.iter(W + 't', W + 'br'))
    if not t: return ''
    rpr = r.find(W + 'rPr'); t = html.escape(t).replace('\n', '<br>')
    if rpr is not None and rpr.find(W + 'b') is not None: t = f'<b>{t}</b>'
    if rpr is not None and rpr.find(W + 'i') is not None: t = f'<i>{t}</i>'
    return t

def para_html(p, part):
    """Walks the paragraph's children in order; w:hyperlink becomes <a href> with the real target from .rels
    (video titles, repositories etc. are hyperlinks with no visible URL: without this the link disappears in the Google Doc)."""
    out = []
    for child in p._p:
        tag = child.tag
        if tag == W + 'r':
            out.append(run_txt(child))
        elif tag == W + 'hyperlink':
            txt = ''.join(run_txt(r) for r in child.iter(W + 'r'))
            if not txt: continue
            rid = child.get(R + 'id'); target = ''
            if rid:
                rel = part.rels.get(rid)
                target = rel.target_ref if rel is not None else ''
            out.append(f'<a href="{html.escape(target, quote=True)}">{txt}</a>' if target else txt)
    return ''.join(out)

def convert(path):
    d = docx.Document(path)
    out = ['<html><head><meta charset="utf-8"><style>body{font-family:Arial;font-size:10.5pt}p{margin:0 0 2pt 0}'
           '.h{font-size:14pt}.s{font-size:6pt}.i{margin-left:36pt}</style></head><body>']
    for p in d.paragraphs:
        txt = para_html(p, d.part)
        if not txt.strip(): out.append('<p class="s">&nbsp;</p>'); continue
        size = next((r.font.size.pt for r in p.runs if r.font.size), None)
        ind = p.paragraph_format.left_indent
        cls = ' '.join(c for c in [('h' if size and size >= 13 else ''), ('i' if ind and ind.pt > 10 else '')] if c)
        if '<a href=' not in txt:
            txt = re.sub(r'(https?://[^\s<),]+)', r'<a href="\1">\1</a>', txt)
        out.append(f'<p{" class=%r" % cls if cls else ""}>{txt}</p>')
    out.append('</body></html>'); return '\n'.join(out)

if __name__ == '__main__':
    open(sys.argv[2], 'w').write(convert(sys.argv[1])); print('ok', sys.argv[2])
