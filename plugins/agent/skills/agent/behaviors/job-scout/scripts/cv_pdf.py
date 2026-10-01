#!/usr/bin/env python3
"""Saves the PDF exported from Google Docs (result of download_file_content that the harness writes to tool-results/*.txt)
and checks it against the source DOCX: same text (normalized), links and pages. Usage:
  cv_pdf.py <tool-result.txt> <out.pdf> <source.docx>"""
import sys, json, base64, re, io
import docx, pypdf
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
def norm(t): return re.sub(r'\s+', ' ', t.replace('’', "'").replace('–', '-')).strip()
res, out_path, source_docx = sys.argv[1:4]
pdf = base64.b64decode(json.load(open(res))['content']); open(out_path, 'wb').write(pdf)
rd = pypdf.PdfReader(io.BytesIO(pdf))
txt_pdf = norm(' '.join(p.extract_text() or '' for p in rd.pages))
d = docx.Document(source_docx)
txt_docx = norm(' '.join(''.join(t.text or '' for t in p._p.iter(W + 't')) for p in d.paragraphs))
links_docx = {d.part.rels[h.get(R + 'id')].target_ref for p in d.paragraphs for h in p._p.iter(W + 'hyperlink') if h.get(R + 'id') in d.part.rels}
links_pdf = set(u.decode(errors='ignore') for u in re.findall(rb'/URI\s*\(([^)]+)\)', pdf))
missing = sorted(l for l in links_docx if l not in links_pdf)
# text: compares word by word (the PDF breaks lines and hyphenates differently)
pd, dd = txt_pdf.replace(' ', ''), txt_docx.replace(' ', '')
print(f'pdf {len(pdf)} bytes, {len(rd.pages)} paginas | texto igual ao DOCX: {pd == dd} '
      f'(pdf {len(pd)} x docx {len(dd)} chars) | links no DOCX {len(links_docx)}, faltando no PDF {len(missing)}')
if pd != dd:
    i = next((k for k in range(min(len(pd), len(dd))) if pd[k] != dd[k]), min(len(pd), len(dd)))
    print('  primeira diferenca ~', i, '| pdf:', pd[max(0,i-40):i+60]); print('  docx:', dd[max(0,i-40):i+60])
for f in missing: print('  link faltando:', f)
