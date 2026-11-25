#!/usr/bin/env python3
"""Compact index of a construction's IFC models, for the `quantities` option of construction-advisor (PLN0968).

IFC models are heavy (100+ MB) and may live on a slow drive: each one is read ONCE into a compact index on the
instance's disk, <instance>/data/ifc-index/<model>/ (meta.json, summary.json, elements.jsonl.gz), rebuilt only when the
size or the mtime of its .ifc changes. Every query reads the index, never the .ifc, and never builds it.

The models come in the command line, so they are edited on the machine only (the `quantities` option is a command):
  --ifc NAME=/abs/path/model.ifc   (repeat for each model; without NAME= the name is the file name)

  ifc_index.py --instance I --ifc ... build [--force]   rebuild the models whose .ifc changed (runs with nice 19)
  ifc_index.py --instance I --ifc ... status            fresh, stale or missing per model (exit 3 if any is not fresh)
  ifc_index.py --instance I --ifc ... summary           storeys and element count per class
  ifc_index.py --instance I --ifc ... class IfcDoor     elements of a class (name, type, storey, room, material, qty)
  ifc_index.py --instance I --ifc ... search TEXT       elements whose name, type, material, brand or property has TEXT
  ifc_index.py --instance I --ifc ... qty CLASS|TEXT    quantities summed per key: Qto keys with their unit (never m
                                                        with m2), custom pset numbers by full name, as in the model
  ifc_index.py --instance I --ifc ... materials | manufacturers | spaces
Options: --model NAME (only that model), --storey X (class, search, qty), --limit N, --json, --keep-prop NAME (build:
one more property name worth keeping, besides the standard ones; kept in meta.json, and a build with a different set
rebuilds the index; status ignores it).

So `quantities` is the whole line ending in `qty --` (the `--` keeps a keyword that starts with `-` from reading as an
option); one quoted keyword, the class or the text, is appended to it.

Extraction (extract_ifc, the only code that touches ifcopenshell, imported only there) is kept apart from the cache
(build): the tests give build a fake extractor. Without ifcopenshell, build prints how to install it and exits 4,
with no traceback; queries over an index already built do not need it.

Exit codes: 0 ok; 1 a model failed (file missing, unreadable, index broken); 2 usage; 3 index missing or stale (status) or missing
(queries); 4 ifcopenshell is not installed.
"""
import argparse, fcntl, gzip, json, os, re, signal, sys, tempfile, time, unicodedata
from collections import Counter, defaultdict

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path: sys.path.insert(0, SCRIPTS)
from watch_core import fileio   # noqa: E402

INDEX_DIR = 'ifc-index'
FORMAT = 1                                  # index layout; a different one is stale
MODEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,60}')
EXIT_FAIL, EXIT_USAGE, EXIT_STALE, EXIT_NO_IFCOPENSHELL = 1, 2, 3, 4
INSTALL_HINT = 'ifcopenshell nao esta instalado: rode `pip install ifcopenshell` e depois o build de novo'

# property names worth keeping from any pset (normalized: lowercase, no space, no underscore): brand, model, system,
# finish, performance, room. Standard IFC names plus their usual Portuguese spellings.
KEEP_PROPS = {'manufacturer', 'fabricante', 'model', 'modelo', 'modelreference', 'modellabel', 'articlenumber',
              'brand', 'marca', 'reference', 'referencia', 'finish', 'acabamento', 'color', 'colour', 'cor',
              'system', 'sistema', 'systemname', 'description', 'descricao', 'material', 'power', 'potencia',
              'nominalpower', 'capacity', 'capacidade', 'coolingcapacity', 'heatingcapacity', 'voltage', 'tensao',
              'flowrate', 'vazao', 'diameter', 'diametro', 'nominaldiameter', 'thickness', 'espessura',
              'fireresistance', 'firerating', 'isexternal', 'loadbearing', 'thermaltransmittance',
              'supplier', 'fornecedor', 'warranty', 'garantia', 'category', 'categoria', 'family', 'familia', 'tipo',
              'sku', 'code', 'codigo', 'lightoutput', 'colortemperature', 'ambiente', 'room', 'zone', 'zona',
              'status'}
# numeric properties of any pset that are quantities (custom psets keep a length outside Qto_)
QTY_WORDS = ('length', 'comprimento', 'area', 'volume', 'weight', 'mass', 'peso', 'width', 'height', 'largura',
             'altura', 'perimeter', 'perimetro', 'depth', 'profundidade', 'count', 'quantidade', 'pecas')
ROOM_KEYS = ('room', 'ambiente')
SKIP_CLASSES = ('IfcSite', 'IfcBuilding', 'IfcBuildingStorey', 'IfcAnnotation', 'IfcGrid')
PORT = 'IfcDistributionPort'                # connection points: counted, never listed (nobody buys a port)
BRAND_WORDS, MODEL_WORDS = ('manufacturer', 'fabricante', 'brand', 'marca'), ('modelreference', 'modellabel', 'model', 'modelo')


class MissingDependency(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------- units

def unit_of(key):
    """Unit of a quantity by its name: m2, m3, kg, un or m; None when the name says nothing."""
    k = norm_key(key.split('.')[-1])
    if 'area' in k: return 'm2'
    if 'volume' in k: return 'm3'
    if any(w in k for w in ('weight', 'mass', 'peso')): return 'kg'
    if any(w in k for w in ('count', 'quantidade', 'pecas')): return 'un'
    if any(w in k for w in ('length', 'comprimento', 'width', 'height', 'largura', 'altura', 'perimeter',
                            'perimetro', 'depth', 'profundidade', 'thickness', 'espessura')): return 'm'
    return None


def norm_key(k):
    return str(k).replace('_', '').replace(' ', '').lower()


def fold(text):
    """Lowercase without accents, for search: 'Tubulação' matches 'tubulacao'."""
    t = unicodedata.normalize('NFKD', str(text))
    return ''.join(c for c in t if not unicodedata.combining(c)).lower()


# ------------------------------------------------------------------------------------------------ extraction (IFC)

def extract_ifc(src, keep_props=()):
    """Reads one .ifc with ifcopenshell. Returns {'schema', 'storeys', 'elements' (iterator of records), 'tool'}.
    A record: id, class, ptype, name, otype, type, tag, storey, space, materials [str], qto {key: float; Qto_ and
    BaseQuantities sets converted to m, m2, m3 by the project units, numbers of custom psets as written},
    props {pset.key: value}, systems [str]. Raises MissingDependency without ifcopenshell."""
    try:
        import ifcopenshell
        import ifcopenshell.util.element as ue
    except ImportError as e:
        raise MissingDependency(str(e)) from None
    keep = KEEP_PROPS | {norm_key(k) for k in keep_props}
    f = ifcopenshell.open(src)
    scale = {'m': 1.0, 'm2': 1.0, 'm3': 1.0}
    try:
        import ifcopenshell.util.unit as uu
        scale = {'m': uu.calculate_unit_scale(f, 'LENGTHUNIT'), 'm2': uu.calculate_unit_scale(f, 'AREAUNIT'),
                 'm3': uu.calculate_unit_scale(f, 'VOLUMEUNIT')}
    except Exception:
        pass
    storeys = {s.id(): (s.Name or '?') for s in f.by_type('IfcBuildingStorey')}
    container = {}
    for rel in f.by_type('IfcRelContainedInSpatialStructure'):
        for e in rel.RelatedElements or []: container[e.id()] = rel.RelatingStructure
    space_info = {}
    for sp in f.by_type('IfcSpace'):
        par = ue.get_aggregate(sp) or container.get(sp.id())
        space_info[sp.id()] = {'name': sp.LongName or sp.Name or '?',
                               'storey': storeys.get(par.id()) if par is not None else None}
    systems = defaultdict(list)
    for rel in f.by_type('IfcRelAssignsToGroup'):
        g = rel.RelatingGroup
        if g is None or not g.is_a('IfcSystem'): continue
        label = g.Name or g.is_a()
        for o in rel.RelatedObjects or []: systems[o.id()].append(label)

    def spatial(x):            # IFC2X3 has no IfcSpatialElement, only IfcSpatialStructureElement
        return x.is_a('IfcSpatialElement') or x.is_a('IfcSpatialStructureElement')

    def psets_of(x):
        """[(pset name, {key: value}, is a quantity set)], the type's sets included (the occurrence wins): a width
        or a quantity often lives only in the type. Quantity sets are the IfcElementQuantity ones; older
        ifcopenshell without the filters: by the name."""
        try:
            return ([(n, d, True) for n, d in ue.get_psets(x, qtos_only=True).items()] +
                    [(n, d, False) for n, d in ue.get_psets(x, psets_only=True).items()])
        except TypeError:
            return [(n, d, n.startswith('Qto_') or n.startswith('BaseQuantities') or 'quantit' in n.lower())
                    for n, d in ue.get_psets(x).items()]

    def where(e):
        c, par = container.get(e.id()), None
        if c is None:
            par = ue.get_aggregate(e)
            while par is not None and c is None:
                c = container.get(par.id()) or (par if spatial(par) else None)
                par = ue.get_aggregate(par) if c is None else None
        if c is None: return None, None
        if c.is_a('IfcSpace'):
            si = space_info.get(c.id(), {})
            return si.get('storey'), si.get('name')
        if c.is_a('IfcBuildingStorey'): return storeys.get(c.id()), None
        return c.Name, None

    def materials(e):
        try:
            mat = ue.get_material(e, should_skip_usage=True)
        except Exception:
            return []
        if mat is None: return []
        if mat.is_a('IfcMaterial'): out = [mat.Name]
        elif mat.is_a('IfcMaterialLayerSet'):
            out = [f'{l.Material.Name if l.Material else "?"} ({round((l.LayerThickness or 0) * scale["m"], 4)} m)'
                   for l in mat.MaterialLayers]
        elif mat.is_a('IfcMaterialConstituentSet'): out = [c.Material.Name for c in mat.MaterialConstituents or [] if c.Material]
        elif mat.is_a('IfcMaterialProfileSet'): out = [p.Material.Name for p in mat.MaterialProfiles or [] if p.Material]
        elif mat.is_a('IfcMaterialList'): out = [x.Name for x in mat.Materials]
        else: out = [getattr(mat, 'Name', None) or mat.is_a()]
        return [x for x in out if x]

    def value(v):
        w = getattr(v, 'wrappedValue', v)
        if isinstance(w, float): return round(w, 4)
        if isinstance(w, (int, str, bool)) or w is None: return w
        return str(w)

    def records():
        for e in f.by_type('IfcProduct'):
            cls = e.is_a()
            if cls in SKIP_CLASSES: continue
            if cls == PORT:
                yield {'class': PORT}
                continue
            storey, space = where(e)
            if cls == 'IfcSpace':
                si = space_info.get(e.id(), {})
                storey, space = si.get('storey'), si.get('name')
            typ = ue.get_type(e)
            try:
                psets = psets_of(e)
            except Exception:
                psets = []
            qto, props = {}, {}
            for pname, pd, is_q in psets:
                for k, v in pd.items():
                    if k == 'id' or v in (None, ''): continue
                    num = isinstance(v, (int, float)) and not isinstance(v, bool)
                    if num and (is_q or any(w in norm_key(k) for w in QTY_WORDS)):
                        key = k if is_q else f'{pname}.{k}'
                        # only quantity sets follow the project units; a number in a custom pset is kept as written
                        qto[key] = round(float(v) * (scale.get(unit_of(key), 1.0) if is_q else 1.0), 4)
                    elif not is_q and norm_key(k) in keep:
                        props[f'{pname}.{k}'] = value(v)
            if typ is not None:
                try:
                    for pname, pd in ue.get_psets(typ).items():
                        for k, v in pd.items():
                            if k == 'id' or v in (None, '') or norm_key(k) not in keep: continue
                            if props.get(f'{pname}.{k}') == value(v): continue     # inherited as is: no twin
                            props.setdefault(f'type:{pname}.{k}', value(v))
                except Exception:
                    pass
            ptype = getattr(e, 'PredefinedType', None)
            yield {'id': e.GlobalId, 'class': cls, 'ptype': str(ptype) if ptype else None, 'name': e.Name,
                   'otype': getattr(e, 'ObjectType', None), 'type': typ.Name if typ is not None else None,
                   'tag': getattr(e, 'Tag', None), 'storey': storey, 'space': space, 'materials': materials(e),
                   'qto': qto, 'props': props, 'systems': systems.get(e.id(), [])}

    return {'schema': f.schema, 'storeys': sorted(set(storeys.values())), 'elements': records(),
            'tool': f'ifcopenshell {getattr(ifcopenshell, "version", "?")}', 'units': scale}


# ------------------------------------------------------------------------------------------------------ the cache

class Model:
    def __init__(self, name, src, base):
        self.name, self.src = name, src
        self.dir = os.path.join(base, name)
        self.meta_path = os.path.join(self.dir, 'meta.json')
        self.summary_path = os.path.join(self.dir, 'summary.json')
        self.elements_path = os.path.join(self.dir, 'elements.jsonl.gz')

    def meta(self):
        try:
            with open(self.meta_path, encoding='utf-8') as fh: return json.load(fh)
        except (OSError, ValueError):
            return None

    def source_stat(self):
        st = os.stat(self.src)
        return {'src': os.path.abspath(self.src), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}

    def state(self, keep_props=None):
        """('fresh' | 'stale' | 'missing', meta). Stale: the .ifc changed, moved or is gone, the layout is old, or
        (keep_props given: build) the extra property names differ from the ones the index was built with."""
        m = self.meta()
        if not m or not os.path.exists(self.elements_path) or not os.path.exists(self.summary_path):
            return 'missing', m
        try:
            s = self.source_stat()
        except OSError:
            return 'stale', m
        same = all(m.get(k) == s[k] for k in ('src', 'size', 'mtime_ns')) and m.get('format') == FORMAT
        if keep_props is not None: same = same and m.get('keep_props', []) == _keep(keep_props)
        return ('fresh' if same else 'stale'), m

    def summary(self):
        with open(self.summary_path, encoding='utf-8') as fh: return json.load(fh)

    def elements(self):
        with gzip.open(self.elements_path, 'rt', encoding='utf-8') as fh:
            for line in fh: yield json.loads(line)


def build(model, extractor=extract_ifc, force=False, keep_props=()):
    """Rebuilds the index of one model when its .ifc changed (or force). Returns (rebuilt: bool, message).
    A failure while reading leaves the previous index as it was (it reads as stale)."""
    os.makedirs(model.dir, exist_ok=True)
    with open(os.path.join(model.dir, '.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)              # two builds of the same model wait for each other
        state, m = model.state(keep_props)
        if state == 'fresh' and not force:
            return False, f'{model.name}: indice em dia ({m.get("elements")} elementos, gerado {m.get("built_at")})'
        t0 = time.time()
        st = model.source_stat()
        data = extractor(model.src, keep_props)
        fd, tmp = tempfile.mkstemp(dir=model.dir, prefix='elements.jsonl.gz.', suffix='.tmp')
        os.close(fd)
        try:
            with gzip.open(tmp, 'wt', encoding='utf-8') as out:
                summary, n, ports = _index(data['elements'], out)
        except BaseException:
            try: os.unlink(tmp)
            except OSError: pass
            raise
        summary['storeys'] = list(data.get('storeys') or [])
        try: os.unlink(model.meta_path)               # meta goes last: a swap cut in the middle reads as missing
        except FileNotFoundError: pass
        _write_json(model.summary_path, summary)
        os.replace(tmp, model.elements_path)
        meta = dict(st, format=FORMAT, model=model.name, schema=data.get('schema'), elements=n, ports=ports,
                    classes=len(summary['by_class']), built_at=time.strftime('%Y-%m-%d %H:%M:%S %z'),
                    build_s=round(time.time() - t0, 1), tool=data.get('tool'), units=data.get('units'),
                    keep_props=_keep(keep_props))
        _write_json(model.meta_path, meta)
        return True, (f'{model.name}: indice gerado, {n} elementos ({ports} portas so contadas), '
                      f'{len(summary["by_class"])} classes, {len(summary["spaces"])} ambientes, {meta["build_s"]} s')


def _keep(keep_props):
    return sorted({norm_key(k) for k in keep_props or ()})


def _last(k):
    return norm_key(str(k).split('.')[-1])


def _index(elements, out):
    """Writes the compact records to `out` (one JSON per line) and returns (summary, elements written, ports)."""
    by_class, by_class_storey = Counter(), defaultdict(Counter)
    qsum = defaultdict(lambda: defaultdict(float))
    mat_count, room_count = Counter(), Counter()
    manu = defaultdict(lambda: {'count': 0, 'classes': Counter()})
    spaces, n, ports = [], 0, 0
    for r in elements:
        cls = r.get('class') or '?'
        by_class[cls] += 1
        if cls == PORT:
            ports += 1
            continue
        rec = {'id': r.get('id'), 'class': cls, 'ptype': r.get('ptype'), 'name': r.get('name'),
               'otype': r.get('otype'), 'type': r.get('type'), 'tag': r.get('tag'), 'storey': r.get('storey'),
               'space': r.get('space'), 'materials': list(r.get('materials') or []),
               'qto': dict(r.get('qto') or {}), 'props': dict(r.get('props') or {}),
               'systems': list(r.get('systems') or [])}
        if not rec['space']:
            rec['space'] = next((str(v) for k, v in rec['props'].items() if _last(k) in ROOM_KEYS), None)
        out.write(json.dumps(rec, ensure_ascii=False) + '\n')
        n += 1
        by_class_storey[cls][rec['storey'] or '-'] += 1
        for k, v in rec['qto'].items(): qsum[cls][k] += v
        for x in rec['materials']: mat_count[x] += 1
        if rec['space'] and cls != 'IfcSpace': room_count[rec['space']] += 1
        brand = next((v for k, v in rec['props'].items() if any(w in _last(k) for w in BRAND_WORDS)), None)
        if brand:
            mdl = next((v for k, v in rec['props'].items() if _last(k) in MODEL_WORDS), None)
            key = f'{brand}' + (f' / {mdl}' if mdl else '')
            manu[key]['count'] += 1
            manu[key]['classes'][cls] += 1
        if cls == 'IfcSpace':
            q = rec['qto']
            spaces.append({'name': rec['space'] or rec['name'], 'code': rec['name'], 'storey': rec['storey'],
                           'area_m2': q.get('NetFloorArea') or q.get('GrossFloorArea'),
                           'perimeter_m': q.get('NetPerimeter') or q.get('GrossPerimeter'),
                           'height_m': q.get('Height') or q.get('FinishCeilingHeight')})
    summary = {
        'by_class': dict(by_class.most_common()),
        'by_class_storey': {c: dict(v) for c, v in sorted(by_class_storey.items())},
        'quantities_by_class': {c: {k: round(v, 3) for k, v in d.items()} for c, d in sorted(qsum.items())},
        'materials': dict(mat_count.most_common()),
        'manufacturers': {k: {'count': v['count'], 'classes': dict(v['classes'])}
                          for k, v in sorted(manu.items(), key=lambda kv: (-kv[1]['count'], kv[0]))},
        'rooms': dict(room_count.most_common()),
        'spaces': sorted(spaces, key=lambda s: (s['storey'] or '', s['name'] or '')),
    }
    return summary, n, ports


def _write_json(path, obj):
    fileio.write_json(path, obj, ensure_ascii=False, indent=1)


def low_priority():
    """The build reads a big file: lowest CPU priority (nice 19), best effort."""
    try: os.nice(19)
    except (OSError, AttributeError): pass


# -------------------------------------------------------------------------------------------------------- queries

def match_fn(needle, storey):
    n = fold(needle.strip())
    by_class = n.startswith('ifc') and ' ' not in n
    # IFC2X3 exports write IfcWallStandardCase, IfcSlabStandardCase...: they count as the base class (a plain prefix
    # would not do: IfcStair would also take IfcStairFlight)
    classes = (n, n + 'standardcase', n + 'elementedcase')
    st = fold(storey) if storey else None

    def text(r):
        return fold(' '.join(str(x) for x in [r['class'], r['ptype'], r['name'], r['otype'], r['type'], r['tag'],
                                               r['space'], r['storey'], *r['materials'], *r['systems'],
                                               *r['props'].values()] if x))

    def ok(r):
        if st and fold(r['storey'] or '') != st: return False
        return r['class'].lower() in classes if by_class else n in text(r)
    return ok


def qty_unit(key):
    """Unit of an indexed quantity: a Qto key (no dot) was converted by the project units, so its name gives the unit;
    a custom pset number (`Pset.Key`) is kept as written in the model, in a unit nobody knows (None)."""
    return None if '.' in key else unit_of(key)


def qty_totals(hits):
    """Sums per quantity key, each with its unit. A custom pset number keeps its whole name (`Pset_X.Length`, as in
    the model) and never adds to the Qto key of the same name (`Length`, in m): keys never merge across units."""
    tot, count = defaultdict(float), defaultdict(int)
    for r in hits:
        for k, v in r['qto'].items():
            tot[k] += v
            count[k] += 1
    return [{'key': k, 'unit': qty_unit(k), 'total': round(tot[k], 3), 'elements': count[k]} for k in sorted(tot)]


def _unit_label(t):
    if t['unit']: return ' ' + t['unit']
    return ' (unidade do modelo)' if '.' in t['key'] else ''


def _num(v, unit):
    return '-' if v is None else f'{v} {unit}'


def short(r, limit_q=5, limit_p=4):
    pt = r['ptype'] if r['ptype'] and r['ptype'] not in ('NOTDEFINED', 'USERDEFINED') else None
    q = ', '.join(f'{k}={v}' for k, v in list(r['qto'].items())[:limit_q])     # whole key: Length is not Pset.Length
    p = '; '.join(f'{k.split(".")[-1]}={v}' for k, v in list(r['props'].items())[:limit_p])
    where = (r['storey'] or '-') + (f' / {r["space"]}' if r['space'] else '')
    return (f"{r['class']}{'/' + pt if pt else ''} | {r['name'] or '-'} | tipo {r['type'] or r['otype'] or '-'} | "
            f"{where} | {', '.join(r['materials'][:3]) or '-'}" + (f' | {q}' if q else '') + (f' | {p}' if p else ''))


def query(model, cmd, arg, a):
    """One model's answer to a query command: (lines, json_value)."""
    if cmd in ('summary', 'materials', 'manufacturers', 'spaces'):
        s = model.summary()
        if cmd == 'summary':
            m = model.meta() or {}
            lines = [f"pavimentos: {', '.join(s['storeys']) or '-'}",
                     f"{m.get('elements', '?')} elementos, {len(s['by_class'])} classes, schema {m.get('schema') or '?'}"]
            for c, n in list(s['by_class'].items())[:a.limit]:
                per = s['by_class_storey'].get(c, {})
                lines.append(f'{c}: {n}' + (' (' + ', '.join(f'{k} {v}' for k, v in per.items()) + ')' if per else ''))
            if len(s['by_class']) > a.limit: lines.append(f'... +{len(s["by_class"]) - a.limit} classes (use --limit)')
            return lines, {'storeys': s['storeys'], 'by_class': s['by_class'], 'by_class_storey': s['by_class_storey']}
        if cmd == 'spaces':
            sp = s['spaces']
            lines = [f"{x['storey'] or '-'} | {x['name']} | area {_num(x['area_m2'], 'm2')} | "
                     f"perimetro {_num(x['perimeter_m'], 'm')} | pe-direito {_num(x['height_m'], 'm')}" for x in sp[:a.limit]]
            if len(sp) > a.limit: lines.append(f'... +{len(sp) - a.limit} (use --limit)')
            return lines or ['(nenhum IfcSpace no modelo)'], sp
        items = list(s[cmd].items())
        if cmd == 'manufacturers':
            lines = [f"{k}: {v['count']} (" + ', '.join(f'{c} {n}' for c, n in v['classes'].items()) + ')'
                     for k, v in items[:a.limit]]
        else:
            lines = [f'{k}: {v}' for k, v in items[:a.limit]]
        if len(items) > a.limit: lines.append(f'... +{len(items) - a.limit} (use --limit)')
        return lines or [f'(nenhum em {cmd})'], s[cmd]
    ok = match_fn(arg, a.storey)
    hits = [r for r in model.elements() if ok(r)]
    where = f' no pavimento {a.storey}' if a.storey else ''
    if cmd == 'qty':
        tot = qty_totals(hits)
        by = Counter((r['class'], r['type'] or r['name'] or '-') for r in hits)
        lines = [f'{len(hits)} elemento(s) com "{arg}"{where}']
        lines += [f"  {t['key']}: {t['total']}{_unit_label(t)} ({t['elements']} el.)" for t in tot]
        lines += [f'  {k} x {c} {t}' for (c, t), k in by.most_common(min(a.limit, 15))]
        if len(by) > min(a.limit, 15): lines.append(f'  ... +{len(by) - min(a.limit, 15)} tipos')
        return lines, {'elements': len(hits), 'totals': tot,
                       'by_type': [{'class': c, 'type': t, 'count': k} for (c, t), k in by.most_common()]}
    lines = [short(r) for r in hits[:a.limit]]
    if len(hits) > a.limit: lines.append(f'... +{len(hits) - a.limit} (use --limit)')
    return lines or [f'nada encontrado para "{arg}"{where}'], {'total': len(hits), 'elements': hits[:a.limit]}


# ------------------------------------------------------------------------------------------------------------ CLI

def parse_models(specs, base):
    out, seen = [], set()
    for spec in specs or []:
        name, sep, path = spec.partition('=')
        if not sep or os.sep in name:
            path = spec
            stem = os.path.splitext(os.path.basename(spec))[0]
            name = re.sub(r'[^A-Za-z0-9_.-]+', '-', stem).strip('-.').lower() or 'modelo'
        if not MODEL_RE.fullmatch(name):
            raise SystemExit(f'nome de modelo invalido: {name!r} (letras, digitos, . _ -)')
        if name in seen: raise SystemExit(f'modelo repetido em --ifc: {name}')
        seen.add(name)
        out.append(Model(name, os.path.expanduser(path), base))
    return out


def index_base(instance):
    import paths
    paths.instance(instance)
    return os.path.join(paths.DATA_DIR, INDEX_DIR)


def main(argv=None, extractor=extract_ifc):
    ap = argparse.ArgumentParser(prog='ifc_index.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--instance', required=True, help='instancia do agent (o indice fica em <instancia>/data/ifc-index)')
    ap.add_argument('--ifc', action='append', metavar='[NOME=]CAMINHO', help='um modelo IFC (repita para varios)')
    ap.add_argument('--model', help='so este modelo')
    ap.add_argument('--storey', help='so este pavimento (class, search, qty)')
    ap.add_argument('--limit', type=int, default=30)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--force', action='store_true', help='build: refaz mesmo em dia')
    ap.add_argument('--keep-prop', action='append', default=[], help='build: mais um nome de propriedade a guardar')
    ap.add_argument('cmd', choices=['build', 'status', 'summary', 'class', 'search', 'qty', 'materials',
                                    'manufacturers', 'spaces'])
    ap.add_argument('arg', nargs='*', help='classe IFC ou texto (class, search, qty)')
    a = ap.parse_args(argv)
    a.limit = max(1, a.limit)
    arg = ' '.join(a.arg).strip()
    models = parse_models(a.ifc, index_base(a.instance))
    if not models:
        print('nenhum modelo IFC: passe --ifc NOME=/caminho/modelo.ifc (um por modelo)', file=sys.stderr)
        return EXIT_USAGE
    if a.model:
        models = [m for m in models if m.name == a.model]
        if not models:
            print(f'modelo desconhecido: {a.model}', file=sys.stderr)
            return EXIT_USAGE
    if a.cmd in ('class', 'search', 'qty') and not arg:
        print(f'{a.cmd}: falta a classe IFC ou o texto', file=sys.stderr)
        return EXIT_USAGE
    out, rc = {}, 0

    def emit(name, lines):
        if a.json: return
        label = f'[{name}] ' if len(models) > 1 else ''
        for line in lines: print(label + line)

    if a.cmd == 'build':
        low_priority()
        for m in models:
            if not os.path.isfile(m.src):
                msg, rc = f'{m.name}: modelo IFC nao encontrado: {m.src}', rc or EXIT_FAIL
                out[m.name] = {'ok': False, 'error': msg}
            else:
                try:
                    rebuilt, msg = build(m, extractor, a.force, a.keep_prop)
                    out[m.name] = {'ok': True, 'rebuilt': rebuilt, 'message': msg}
                except MissingDependency:
                    msg, rc = INSTALL_HINT, EXIT_NO_IFCOPENSHELL
                    out[m.name] = {'ok': False, 'error': msg}
                    if not a.json: print(msg, file=sys.stderr)
                    break
                except Exception as e:                 # one bad model never stops the others nor shows a traceback
                    msg, rc = f'{m.name}: falhou ao ler o modelo: {type(e).__name__}: {e}'[:400], rc or EXIT_FAIL
                    out[m.name] = {'ok': False, 'error': msg}
            if not a.json and out[m.name].get('error') != INSTALL_HINT: print(msg)
    elif a.cmd == 'status':
        for m in models:
            state, meta = m.state()
            meta = meta or {}
            out[m.name] = {'state': state, 'src': m.src, 'elements': meta.get('elements'),
                           'built_at': meta.get('built_at'), 'build_s': meta.get('build_s')}
            if state != 'fresh': rc = EXIT_STALE
            text = {'fresh': 'em dia', 'stale': 'DESATUALIZADO (o .ifc mudou ou sumiu): rodar build',
                    'missing': 'indice nao existe: rodar build'}[state]
            if not a.json:
                extra = f" | {meta['elements']} elementos | gerado {meta.get('built_at')}" if state != 'missing' else ''
                print(f'{m.name}: {text}{extra} | fonte {m.src}')
    else:
        for m in models:
            state, _ = m.state()
            if state == 'missing':
                msg = f'{m.name}: indice IFC ainda nao existe: rode o build (ifc_index.py ... build)'
                print(msg, file=sys.stderr)
                out[m.name] = {'error': msg}
                rc = EXIT_STALE
                continue
            if state == 'stale':
                print(f'aviso: indice de {m.name} desatualizado (o .ifc mudou ou sumiu): rode o build', file=sys.stderr)
            try:
                lines, value = query(m, a.cmd, arg, a)
            except Exception as e:                     # a broken index (truncated .gz) never stops the other models
                msg = f'{m.name}: indice IFC ilegivel ({type(e).__name__}: {e}): rode o build com --force'[:400]
                print(msg, file=sys.stderr)
                out[m.name] = {'error': msg}
                rc = rc or EXIT_FAIL
                continue
            out[m.name] = value if not a.json else {'stale': state == 'stale', 'result': value}
            emit(m.name, lines)
    if a.json:
        print(json.dumps({'cmd': a.cmd, 'arg': arg or None, 'models': out}, ensure_ascii=False))
    return rc


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
