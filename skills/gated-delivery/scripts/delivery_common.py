"""Local evidence integrity checks, not a business oracle or authorization engine."""
from pathlib import Path
import hashlib
import json
import re
import xml.etree.ElementTree as ET

LEVELS = {'unit', 'integration', 'ui', 'production'}

def need(value, message):
    if not value:
        raise ValueError(message)

def path_in(root, name, *, exists=False):
    p = Path(name)
    need(isinstance(name, str) and name and not p.is_absolute() and '..' not in p.parts, 'relative path required')
    root = Path(root).resolve(strict=True)
    current = root
    for part in p.parts:
        current = current / part
        need(not current.is_symlink(), 'symlink is outside this evidence contract')
    need(current.resolve().is_relative_to(root), 'path escapes project root')
    if exists:
        need(current.is_file(), 'required file absent: ' + name)
    return current

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def json_read(path):
    need(Path(path).stat().st_size <= 1024 * 1024, 'JSON metadata exceeds1MiB')
    return json.loads(Path(path).read_text())

def json_new(path, value):
    with Path(path).open('x') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')

def snapshot(root, names):
    result = {}
    for name in names:
        p = path_in(root, name)
        need(not p.exists() or p.is_file(), 'source must be a file')
        result[name] = {'exists': p.exists(), 'sha256': digest(p) if p.exists() else None,
                        'bytes': p.stat().st_size if p.exists() else None}
    return result

def load_plan(root, filename):
    p = path_in(root, filename, exists=True)
    v = json_read(p)
    need(v.get('schema_version') == 1 and isinstance(v.get('task_id'), str) and v['task_id'], 'plan identity required')
    files = v.get('source_files')
    need(isinstance(files, list) and files and len(files) == len(set(files)), 'explicit unique source_files required')
    for name in files: path_in(root, name)
    oracle = v.get('oracle_files', [])
    need(isinstance(oracle, list) and set(oracle) <= set(files), 'oracle_files must be included in source_files')
    criteria = v.get('criteria', [])
    checks = v.get('checks', [])
    need(criteria and checks, 'criteria and checks required')
    for group in (criteria, checks):
        need(all(isinstance(x, dict) and isinstance(x.get('id'), str) for x in group), 'stable IDs required')
        need(len({x['id'] for x in group}) == len(group), 'duplicate ID')
    by_id = {x['id']: x for x in checks}
    for c in checks:
        need(c.get('kind') in {'test', 'command'} and c.get('level') in LEVELS, 'check kind/level invalid')
        cmd = c.get('command')
        need(isinstance(cmd, list) and cmd and all(isinstance(x, str) and x for x in cmd), 'explicit argv vector required')
        need(type(c.get('timeout_seconds')) is int and c['timeout_seconds'] > 0, 'positive check timeout required')
        path_in(root, c['evidence'])
        need(type(c.get('required', True)) is bool, 'required must be boolean')
        if c['kind'] == 'test':
            need(c.get('report', {}).get('format') in {'unittest', 'junit'}, 'test count parser required')
            if c['report']['format'] == 'junit': path_in(root, c['report']['path'])
    for a in criteria:
        need(a.get('kind') in {'positive', 'negative'} and a.get('level') in LEVELS, 'criterion kind/level invalid')
        need(type(a.get('required', True)) is bool, 'required must be boolean')
        ids = a.get('test_ids', [])
        need(isinstance(ids, list) and ids and set(ids) <= set(by_id), 'criterion must map to declared checks')
        need(any(by_id[t]['level'] == a['level'] for t in ids), 'criterion evidence level not covered')
        if a.get('requires_red'):
            need(oracle and any(by_id[t]['kind'] == 'test' for t in ids), 'RED requires explicit oracle_files and test')
    if v.get('side_effecting'):
        need(any(a['kind'] == 'negative' and a.get('required', True) for a in criteria), 'side effects require a negative criterion')
        need(all(v.get(k) for k in ('authorized_actions', 'protected', 'stop_conditions')), 'scope/protected/stop conditions required')
    return v, digest(p)

def test_counts(check, root, stdout, stderr, report_override=None):
    if check['kind'] != 'test': return None
    if check['report']['format'] == 'unittest':
        text = stdout.read_text(errors='replace') + '\n' + stderr.read_text(errors='replace')
        runs = re.findall(r'^Ran (\d+) tests? in ', text, re.M)
        need(len(runs) == 1, 'one actual unittest summary required')
        total = int(runs[0])
        def number(key):
            m = re.search(r'\b' + key + r'=(\d+)', text)
            return int(m.group(1)) if m else 0
        return {'tests': total, 'failures': number('failures'), 'errors': number('errors'),
                'skipped': number('skipped'), 'xfail': number('expected failures')}
    report = report_override or path_in(root, check['report']['path'], exists=True)
    need(report.stat().st_size <= 5 * 1024**2, 'JUnit report too large')
    raw = report.read_bytes()
    need(b'<!DOCTYPE' not in raw and b'<!ENTITY' not in raw, 'DTD not permitted')
    tree = ET.fromstring(raw)
    suites = [x for x in tree.iter('testsuite') if not list(x.iterfind('testsuite'))]
    need(suites, 'JUnit testsuite absent')
    result = dict.fromkeys(('tests', 'failures', 'errors', 'skipped', 'xfail'), 0)
    for suite in suites:
        cases = list(suite.iterfind('testcase'))
        observed = {'tests': len(cases), 'failures': sum(x.find('failure') is not None for x in cases),
                    'errors': sum(x.find('error') is not None for x in cases),
                    'skipped': sum(x.find('skipped') is not None for x in cases)}
        need(all(int(suite.get(k, '0')) == n for k, n in observed.items()), 'JUnit count does not match testcase nodes')
        for k, n in observed.items(): result[k] += n
    return result

def verify_receipt(root, plan, plan_sha, check, pointer):
    relative = pointer['receipt']
    folder = Path(check['evidence']) / 'runs'
    need(Path(relative).is_relative_to(folder) and Path(relative).name == 'receipt.json', 'receipt outside declared check runs')
    p = path_in(root, relative, exists=True)
    need(digest(p) == pointer['sha256'], 'receipt bytes changed')
    r = json_read(p)
    need(r['schema_version'] == 1 and r['task_id'] == plan['task_id'] and r['check_id'] == check['id'], 'receipt identity mismatch')
    need(r['plan_sha256'] == plan_sha and r['command'] == check['command'], 'plan or command changed')
    need(r['kind'] == check['kind'] and r['level'] == check['level'], 'evidence kind/level mismatch')
    for key in ('stdout', 'stderr'):
        item = r[key]; f = path_in(root, item['path'], exists=True)
        need(f.parent == p.parent and digest(f) == item['sha256'] and f.stat().st_size == item['bytes'], 'raw check output changed')
    if check['kind'] == 'test':
        need(r['counts'] and r['counts']['tests'] > 0, 'zero tests is not PASS')
        report = None
        if check['report']['format'] == 'junit':
            item = r['report']; report = path_in(root, item['path'], exists=True)
            need(report.parent == p.parent and digest(report) == item['sha256'] and report.stat().st_size == item['bytes'], 'archived test report changed')
        observed = test_counts(check, root, path_in(root, r['stdout']['path']), path_in(root, r['stderr']['path']), report)
        need(r['counts'] == observed, 'receipt counts differ from original test report')
    return r

def evaluate(root, planfile):
    plan, plan_sha = load_plan(root, planfile)
    current = snapshot(root, plan['source_files'])
    results = {}; problems = []
    required_ids = {t for a in plan['criteria'] if a.get('required', True) for t in a['test_ids']}
    required_ids |= {x['id'] for x in plan['checks'] if x.get('required', True)}
    for check in plan['checks']:
        try:
            pointer = json_read(path_in(root, check['evidence'] + '/latest.json', exists=True))
            runs = path_in(root, check['evidence'] + '/runs')
            folders = list(runs.iterdir())
            need(folders and all(re.fullmatch(r'\d{8}T\d{12}Z-[0-9a-f]{8}', x.name) and x.is_dir() and not x.is_symlink() for x in folders), 'invalid or symlink run directory')
            newest = max(folders, key=lambda x: x.name)
            need(Path(pointer['receipt']).parent == newest.relative_to(root), 'latest pointer does not name newest run; unfinished runs also block')
            r = verify_receipt(root, plan, plan_sha, check, pointer)
            need(r['state'] == 'passed' and r['returncode'] == 0 and not r['stop_reason'], 'check did not pass')
            need(r['source_before'] == current == r['source_after'], 'receipt is for different source bytes')
            if r['counts']:
                need(all(r['counts'][k] == 0 for k in ('failures', 'errors', 'skipped', 'xfail')), 'failed/skipped/xfail tests cannot satisfy this gate')
            results[check['id']] = r
        except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as e:
            if check['id'] in required_ids: problems.append(check['id'] + ': ' + str(e))
    for a in plan['criteria']:
        if not a.get('required', True) or not a.get('requires_red'): continue
        found = False
        for tid in a['test_ids']:
            if tid not in results: continue
            check = next(x for x in plan['checks'] if x['id'] == tid)
            for old in path_in(root, check['evidence']).glob('runs/*/receipt.json'):
                try:
                    r = verify_receipt(root, plan, plan_sha, check, {'receipt': str(old.relative_to(root)), 'sha256': digest(old)})
                    if r['state'] == 'failed' and r['returncode'] != 0 and r['stop_reason'] == 'test_counts_not_clean' and r['counts'] and r['counts']['failures'] > 0 and not r['counts']['errors'] and r['oracle_snapshot'] == results[tid]['oracle_snapshot'] and r['started_at'] < results[tid]['started_at']:
                        found = True
                except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError): pass
        if not found: problems.append(a['id'] + ': no prior actual assertion RED with unchanged oracle')
    return {'state': 'PASSED' if not problems else 'REJECTED', 'task_id': plan['task_id'], 'problems': problems,
            'required_criteria': sum(a.get('required', True) for a in plan['criteria']),
            'passed_checks': sorted(results), 'business_oracle_or_production_permission_proved': False}
