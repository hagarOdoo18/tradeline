#!/usr/bin/env python3
import ast
import csv
import json
from pathlib import Path
import configparser
import psycopg2
import psycopg2.extras

BASE = Path('/tmp/odoo_snapshots/missing_modules_analysis')
BASE.mkdir(parents=True, exist_ok=True)

missing_path = Path('/tmp/odoo_migration_tools/missing_installed_modules_odoo12.json')
mods = json.loads(missing_path.read_text(encoding='utf-8'))
mods_set = set(mods)

# read addons_path from odoo config
cfg = configparser.RawConfigParser()
cfg.read('/etc/odoo12.conf')
addons_paths = []
if cfg.has_option('options', 'addons_path'):
    addons_paths = [p.strip() for p in cfg.get('options', 'addons_path').split(',') if p.strip()]

conn = psycopg2.connect(dbname='live_11nov_2024', user='odoo12', host='/var/run/postgresql')
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute('''
SELECT m.name, m.shortdesc, m.summary, m.description, m.author, m.website, m.latest_version,
       m.state, c.name AS category
FROM ir_module_module m
LEFT JOIN ir_module_category c ON c.id = m.category_id
WHERE m.name = ANY(%s)
ORDER BY m.name
''', (mods,))
module_meta = {r['name']: dict(r) for r in cur.fetchall()}

cur.execute('''
SELECT imd.module, COUNT(*)::int AS model_count
FROM ir_model_data imd
WHERE imd.model = 'ir.model' AND imd.module = ANY(%s)
GROUP BY imd.module
''', (mods,))
model_counts = {r['module']: int(r['model_count']) for r in cur.fetchall()}

cur.execute('''
SELECT imd.module,
       COUNT(*)::int AS field_count,
       COUNT(*) FILTER (WHERE COALESCE(f.required, false))::int AS required_field_count
FROM ir_model_data imd
JOIN ir_model_fields f ON f.id = imd.res_id
WHERE imd.model = 'ir.model.fields' AND imd.module = ANY(%s)
GROUP BY imd.module
''', (mods,))
field_counts = {r['module']: {'field_count': int(r['field_count']), 'required_field_count': int(r['required_field_count'])} for r in cur.fetchall()}

cur.execute('''
SELECT imd.module, m.model, m.name, COALESCE(m.transient,false) AS transient, COALESCE(m.state,'') AS state
FROM ir_model_data imd
JOIN ir_model m ON m.id = imd.res_id
WHERE imd.model = 'ir.model' AND imd.module = ANY(%s)
ORDER BY imd.module, m.model
''', (mods,))
model_rows = [dict(r) for r in cur.fetchall()]

cur.execute('''
SELECT imd.module, f.model, f.name, f.field_description, f.ttype,
       COALESCE(f.required,false) AS required,
       COALESCE(f.store,false) AS store,
       COALESCE(f.state,'') AS state,
       COALESCE(f.relation,'') AS relation
FROM ir_model_data imd
JOIN ir_model_fields f ON f.id = imd.res_id
WHERE imd.model = 'ir.model.fields' AND imd.module = ANY(%s)
ORDER BY imd.module, f.model, f.name
''', (mods,))
field_rows = [dict(r) for r in cur.fetchall()]

cur.close()
conn.close()

# manifests

def parse_manifest(path: Path):
    if not path.exists():
        return None
    try:
        txt = path.read_text(encoding='utf-8')
    except Exception:
        try:
            txt = path.read_text(encoding='latin-1')
        except Exception:
            return None
    try:
        return ast.literal_eval(txt)
    except Exception:
        return None

manifest_data = {}
for mod in mods:
    found = None
    for ap in addons_paths:
        d = Path(ap) / mod
        if d.exists() and d.is_dir():
            m1 = d / '__manifest__.py'
            m2 = d / '__openerp__.py'
            md = parse_manifest(m1) or parse_manifest(m2)
            found = {
                'module': mod,
                'path': str(d),
                'manifest_file': '__manifest__.py' if m1.exists() else ('__openerp__.py' if m2.exists() else None),
                'manifest': md,
            }
            break
    manifest_data[mod] = found

# build matrix rows
rows = []
for mod in sorted(mods):
    mm = module_meta.get(mod, {})
    mf = manifest_data.get(mod)
    m = (mf or {}).get('manifest') or {}

    depends = m.get('depends') if isinstance(m.get('depends'), list) else []
    data_files = m.get('data') if isinstance(m.get('data'), list) else []
    demo_files = m.get('demo') if isinstance(m.get('demo'), list) else []

    purpose = (m.get('summary') or m.get('description') or mm.get('summary') or mm.get('description') or mm.get('shortdesc') or '').strip()
    purpose = ' '.join(purpose.split())

    fc = field_counts.get(mod, {'field_count': 0, 'required_field_count': 0})

    row = {
        'module': mod,
        'state': mm.get('state') or 'unknown',
        'category': m.get('category') or mm.get('category') or '',
        'shortdesc': m.get('name') or mm.get('shortdesc') or '',
        'summary': m.get('summary') or mm.get('summary') or '',
        'description': (m.get('description') or mm.get('description') or ''),
        'purpose': purpose,
        'author': m.get('author') or mm.get('author') or '',
        'website': m.get('website') or mm.get('website') or '',
        'version': m.get('version') or mm.get('latest_version') or '',
        'depends_count': len(depends),
        'depends': ', '.join(depends),
        'data_files_count': len(data_files),
        'demo_files_count': len(demo_files),
        'model_count': model_counts.get(mod, 0),
        'field_count': fc.get('field_count', 0),
        'required_field_count': fc.get('required_field_count', 0),
        'manifest_found': bool(mf and mf.get('manifest') is not None),
        'module_path': (mf or {}).get('path', ''),
    }
    rows.append(row)

# csv/json outputs
matrix_csv = BASE / 'missing_modules_matrix.csv'
matrix_json = BASE / 'missing_modules_matrix.json'
models_csv = BASE / 'missing_modules_models.csv'
fields_csv = BASE / 'missing_modules_fields.csv'

with matrix_csv.open('w', encoding='utf-8', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

matrix_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')

if model_rows:
    with models_csv.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(model_rows[0].keys()))
        w.writeheader()
        w.writerows(model_rows)
else:
    models_csv.write_text('', encoding='utf-8')

if field_rows:
    with fields_csv.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(field_rows[0].keys()))
        w.writeheader()
        w.writerows(field_rows)
else:
    fields_csv.write_text('', encoding='utf-8')

# quick summary
from collections import Counter
prefix_counter = Counter()
for mod in mods:
    p = mod.split('_', 1)[0].lower() if '_' in mod else mod.lower()
    prefix_counter[p] += 1
summary = {
    'missing_installed_module_count': len(mods),
    'matrix_rows': len(rows),
    'models_rows': len(model_rows),
    'fields_rows': len(field_rows),
    'manifest_found_count': sum(1 for r in rows if r['manifest_found']),
    'top_prefixes': prefix_counter.most_common(20),
}
(BASE / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False))