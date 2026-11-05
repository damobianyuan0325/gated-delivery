#!/usr/bin/env python3
"""Local, stdlib-only installer for gated-delivery. Default: prepare only."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import uuid

SKILLS = ('gated-delivery',)
SCHEMA = 'owned-skills-install.v1'
_TEST_ROOTS = {}


class InstallError(RuntimeError):
    pass


def need(ok, message):
    if not ok:
        raise InstallError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def selected_skills(names):
    need(type(names) in (list, tuple) and names and all(type(x) is str and x in SKILLS for x in names), 'nonempty_owned_skill_subset_required')
    need(len(set(names)) == len(names), 'duplicate_skill_selection')
    return tuple(x for x in SKILLS if x in names)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def canonical(value):
    text = os.fspath(value)
    need(os.path.isabs(text) and not any(x in ('.', '..') for x in text.split('/')), 'absolute_nontraversing_path_required')
    path = Path(text)
    need(path.resolve(strict=False) == path, 'symlink_or_path_alias')
    for ancestor in (path, *path.parents):
        need(not ancestor.is_symlink(), 'symlink_parent')
    return path


def relative(value):
    need(type(value) is str and '\\' not in value and '\0' not in value, 'invalid_relative_path')
    path = PurePosixPath(value)
    need(not path.is_absolute() and str(path) == value and all(x not in ('', '.', '..') for x in path.parts), 'relative_path_escape')
    need(path.parts[0] in SKILLS, 'unowned_skill_path')
    return path


def disjoint(a, b):
    return a != b and a not in b.parents and b not in a.parents


def mode(path):
    return stat.S_IMODE(path.lstat().st_mode)


def directory(path):
    canonical(path)
    need(path.is_dir() and not path.is_symlink(), 'real_directory_required')
    s = path.stat()
    return {'mode': stat.S_IMODE(s.st_mode), 'dev': s.st_dev, 'ino': s.st_ino}


def read_file(path):
    canonical(path)
    s = path.lstat()
    need(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and not stat.S_IMODE(s.st_mode) & 0o7000, 'regular_single_link_file_required')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        need((before.st_dev, before.st_ino) == (s.st_dev, s.st_ino), 'file_changed_before_read')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            raw = f.read()
        after = os.fstat(fd)
    finally:
        os.close(fd)
    end = path.lstat()
    identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns, x.st_mode, x.st_nlink)
    need(identity(s) == identity(after) == identity(end), 'file_changed_during_read')
    return raw, {'bytes': len(raw), 'sha256': digest(raw), 'mode': stat.S_IMODE(s.st_mode),
                 'dev': s.st_dev, 'ino': s.st_ino, 'mtime_ns': s.st_mtime_ns, 'ctime_ns': s.st_ctime_ns}


def file_state(path):
    canonical(path)
    if not path.exists():
        return {'exists': False}
    _, info = read_file(path)
    return {'exists': True, **info}


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def new_file(path, raw, permissions=0o600, *, _ownership=None):
    canonical(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), permissions)
    if _ownership is not None:
        created = os.fstat(fd)
        _ownership.update({'owned_dev':created.st_dev, 'owned_ino':created.st_ino})
    with os.fdopen(fd, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fchmod(f.fileno(), permissions)
        os.fsync(f.fileno())
    sync_dir(path.parent)


def source_tree(root, skills=SKILLS):
    skills = selected_skills(skills)
    directory(root)
    files, dirs = {}, {}
    def visit(path):
        rel = path.relative_to(root).as_posix()
        relative(rel)
        if path.is_symlink():
            raise InstallError('source_symlink')
        if path.is_dir():
            dirs[rel] = directory(path)['mode']
            for child in sorted(path.iterdir()):
                if child.name in ('__pycache__', '.DS_Store', '.git') or child.suffix in ('.pyc', '.pyo'):
                    continue
                visit(child)
        else:
            _, info = read_file(path)
            files[rel] = info
    for name in skills:
        need((root / name / 'SKILL.md').is_file(), 'selected_SKILL_files_required')
        visit(root / name)
    return files, dirs


def paths(source, target, backup):
    source, target, backup = map(canonical, (source, target, backup))
    need(source.name == 'skills', 'source_must_be_skills_directory')
    repo = source.parent
    need(disjoint(repo, target) and disjoint(repo, backup) and disjoint(target, backup), 'source_target_backup_overlap')
    need(not any(x in ('.system', 'plugins', '.plugins') for x in target.parts), 'bundled_or_plugin_target_forbidden')
    directory(target.parent)
    directory(backup.parent)
    if target.exists():
        directory(target)
    return source, target, backup


def target_dirs(target, source_dirs):
    result = {}
    for rel in [''] + sorted(source_dirs, key=lambda x: (len(PurePosixPath(x).parts), x)):
        path = target / rel
        canonical(path)
        result[rel] = {'exists': True, **directory(path)} if path.exists() else {'exists': False}
    return result


def unplanned(target, planned_files, planned_dirs, skills=SKILLS):
    extras = []
    for skill in selected_skills(skills):
        top = target / skill
        if not top.exists():
            continue
        for path in sorted(top.rglob('*')):
            canonical(path)
            need(not path.is_symlink(), 'target_symlink')
            need(path.is_dir() or path.is_file(), 'target_special_file')
            rel = path.relative_to(target).as_posix()
            if rel not in planned_files and rel not in planned_dirs:
                extras.append(rel)
    return extras


def prepare(source, target, backup, skills=SKILLS):
    skills = selected_skills(skills)
    source, target, backup = paths(source, target, backup)
    need(not backup.exists(), 'backup_bundle_must_be_new')
    files, dirs = source_tree(source, skills)
    before_dirs = target_dirs(target, dirs)
    extras = unplanned(target, files, dirs, skills)
    before = {rel: file_state(target / rel) for rel in files}
    backup.mkdir(mode=0o700)
    sync_dir(backup.parent)
    plan_files = []
    for number, rel in enumerate(sorted(files)):
        raw, info = read_file(source / rel)
        need(info == files[rel], 'source_drift_during_prepare')
        after_rel = 'after-%06d.bin' % number
        new_file(backup / after_rel, raw)
        old = before[rel]
        before_rel = None
        if old['exists']:
            original, observed = read_file(target / rel)
            need({'exists': True, **observed} == old, 'target_drift_during_prepare')
            before_rel = 'before-%06d.bin' % number
            new_file(backup / before_rel, original)
        plan_files.append({'path': rel, 'source': info, 'before': old, 'after_snapshot': after_rel,
                           'before_snapshot': before_rel,
                           'action': 'noop' if old['exists'] and all(old[k] == info[k] for k in ('bytes', 'sha256', 'mode')) else 'install'})
    need(source_tree(source, skills) == (files, dirs), 'source_drift_before_plan_freeze')
    need(target_dirs(target, dirs) == before_dirs and all(file_state(target / rel) == old for rel, old in before.items()), 'target_drift_before_plan_freeze')
    plan = {'schema': SCHEMA, 'id': uuid.uuid4().hex, 'tool_sha256': digest(Path(__file__).read_bytes()),
            'source_root': str(source), 'target_root': str(target), 'backup_root': str(backup), 'skills': list(skills),
            'source_files': files, 'source_dirs': dirs, 'target_dirs_before': before_dirs, 'files': plan_files,
            'unplanned_target_paths_reported_preserved': extras, 'restore_scope': 'planned file bytes/mode/mtime and installer-created empty directories; inode/ctime not recreated'}
    raw = json_bytes(plan)
    new_file(backup / 'plan.json', raw)
    new_file(backup / 'plan.sha256', (digest(raw) + '\n').encode())
    load_plan(backup / 'plan.json')
    return {'state': 'PREPARED_DRY_RUN_TARGET_UNCHANGED', 'plan': str(backup / 'plan.json'), 'plan_sha256': digest(raw), 'files': len(plan_files), 'unplanned_preserved': len(extras)}


def load_plan(plan_path):
    plan_path = canonical(plan_path)
    need(plan_path.name == 'plan.json', 'exact_plan_name_required')
    raw, _ = read_file(plan_path)
    seal, _ = read_file(plan_path.with_name('plan.sha256'))
    need(seal.decode().strip() == digest(raw), 'plan_seal_mismatch')
    p = json.loads(raw)
    skills = selected_skills(p['skills'])
    need(p['schema'] == SCHEMA and p['skills'] == list(skills) and p['tool_sha256'] == digest(Path(__file__).read_bytes()), 'schema_allowlist_or_tool_changed')
    source, target, backup = paths(p['source_root'], p['target_root'], p['backup_root'])
    need(plan_path == backup / 'plan.json' and list(sorted(p['source_files'])) == [x['path'] for x in p['files']], 'plan_file_domain_mismatch')
    for row in p['files']:
        need(relative(row['path']).parts[0] in skills, 'path_outside_selected_skills')
        need(row['source'] == p['source_files'][row['path']] and row['action'] in ('install', 'noop'), 'source_plan_mismatch')
        for key, expected in [('after_snapshot', row['source']), ('before_snapshot', row['before'])]:
            name = row[key]
            need((name is not None) == (key == 'after_snapshot' or row['before']['exists']), 'snapshot_presence')
            if name:
                need(Path(name).name == name and name not in ('.', '..'), 'snapshot_escape')
                data, _ = read_file(backup / name)
                need(len(data) == expected['bytes'] and digest(data) == expected['sha256'], 'independent_snapshot_changed')
    for rel in p['source_dirs']:
        need(relative(rel).parts[0] in skills, 'directory_outside_selected_skills')
    need(set(p['target_dirs_before']) == {''} | set(p['source_dirs']), 'target_directory_domain_mismatch')
    allowed = {'plan.json', 'plan.sha256', 'WAL.jsonl'} | {x[k] for x in p['files'] for k in ('after_snapshot','before_snapshot') if x[k]}
    need(all(x.name in allowed and x.is_file() and not x.is_symlink() for x in backup.iterdir()), 'unknown_or_symlink_backup_entry')
    return p


def record(backup, event, **facts):
    path = backup / 'WAL.jsonl'
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'ab') as f:
        f.write(json_bytes({'event': event, **facts}).replace(b'\n', b'') + b'\n')
        f.flush()
        os.fsync(f.fileno())
    sync_dir(backup)


def known(path, state):
    try:
        current = file_state(path)
    except (OSError, InstallError):
        return False
    if not state.get('exists', True):
        return not current['exists']
    return current['exists'] and all(current[k] == state[k] for k in ('bytes', 'sha256', 'mode'))


def atomic_bytes(target, raw, permissions, temp, before_state, *, ownership=None, ready=None):
    canonical(target.parent)
    new_file(temp, raw, permissions, _ownership=ownership)
    if ready is not None:
        ready()  # Durable staged inode ownership precedes the target replacement.
    need((file_state(target) == before_state) if 'exists' in before_state else known(target, before_state), 'target_changed_at_replace_boundary')
    os.replace(temp, target)
    sync_dir(target.parent)


def events(backup):
    raw, _ = read_file(backup / 'WAL.jsonl')
    need(raw.endswith(b'\n'), 'partial_WAL_preserved_manual_review')
    return [json.loads(line) for line in raw.splitlines()]


def parent_domain_matches(p, rel, created):
    target = Path(p['target_root'])
    own = {x['path']:x for x in created}
    prefixes = [''] + ['/'.join(PurePosixPath(rel).parts[:i]) for i in range(1,len(PurePosixPath(rel).parts))]
    try:
        for prefix in prefixes:
            before = p['target_dirs_before'][prefix]
            expected = before if before['exists'] else own.get(prefix)
            need(expected is not None, 'created_parent_ownership_unknown')
            need(directory(target / prefix)=={k:expected[k]for k in ('mode','dev','ino')}, 'parent_directory_replaced')
    except (OSError, InstallError):
        return False
    return True


def restore(p, *, _created=None, _intents=None):
    target, backup = Path(p['target_root']), Path(p['backup_root'])
    records = events(backup) if _created is None else []
    conflicts = []
    def journal(event, **facts):
        try:
            record(backup, event, **facts)
        except OSError:
            if '<journal-write-failed>' not in conflicts:
                conflicts.append('<journal-write-failed>')
    journal('RESTORE_BEGIN')
    created = [x for x in records if x['event'] == 'CREATED_DIRECTORY'] if _created is None else _created
    intents = [dict(x, ready=True) for x in records if x['event'] == 'FILE_STAGED'] if _intents is None else _intents
    owned = {x['path']: x for x in intents if x.get('ready')}
    for row in reversed(p['files']):
        path = target / row['path']
        before = row['before']
        if known(path, before):
            continue
        item = owned.get(row['path'])
        try:
            current = file_state(path)
            attributed = item is not None and current.get('exists') and all(current[k] == item['owned_' + k] for k in ('dev', 'ino'))
        except (OSError, InstallError):
            current, attributed = None, False
        if not attributed or not parent_domain_matches(p, row['path'], created) or not known(path, row['source']):
            conflicts.append(row['path'])
            continue
        if before['exists']:
            data, _ = read_file(backup / row['before_snapshot'])
            temp = path.with_name('.' + path.name + '.restore-' + p['id'])
            try:
                atomic_bytes(path, data, before['mode'], temp, current)
                os.utime(path, ns=(before['mtime_ns'], before['mtime_ns']), follow_symlinks=False)
                sync_dir(path.parent)
            except (OSError, InstallError):
                conflicts.append(row['path'])
                continue
        else:
            if file_state(path) == current:
                path.unlink()
                sync_dir(path.parent)
            else:
                conflicts.append(row['path'])
                continue
        journal('RESTORED_FILE', path=row['path'])
    # Only exact recorded owned staging files can be removed; unknown bytes remain.
    for item in intents:
        if item['event'] in ('FILE_INTENT', 'FILE_STAGED'):
            tmp = target / item['temporary']
            if tmp.exists():
                row = next(x for x in p['files'] if x['path'] == item['path'])
                owned = item.get('owned_dev') is not None and not tmp.is_symlink() and (tmp.lstat().st_dev,tmp.lstat().st_ino)==(item['owned_dev'],item['owned_ino'])
                if owned and known(tmp, row['source']):
                    tmp.unlink(); sync_dir(tmp.parent)
                else:
                    conflicts.append(item['temporary'])
    for item in reversed(created):
        rel = item['path']
        path = target / rel
        try:
            canonical(path)
            if path.exists():
                need(path.is_dir() and all(directory(path)[k] == item[k] for k in ('dev', 'ino', 'mode')), 'created_directory_replaced')
                path.rmdir()  # Never recursively remove an unknown nonempty directory.
                sync_dir(path.parent)
        except (OSError, InstallError):
            conflicts.append(rel or '<target-root>')
    for row in p['files']:
        if not known(target / row['path'], row['before']):
            conflicts.append(row['path'])
    for rel, before in p['target_dirs_before'].items():
        path = target / rel
        try:
            if before['exists']:
                need(directory(path) == {k:before[k] for k in ('mode','dev','ino')}, 'original_directory_changed')
            else:
                need(not path.exists() and not path.is_symlink(), 'new_directory_not_removed')
        except (OSError, InstallError):
            conflicts.append(rel or '<target-root>')
    state = 'RESTORED_BEFORE' if not conflicts else 'RESTORE_PARTIAL_PRESERVED_UNKNOWN'
    journal(state, conflicts=sorted(set(conflicts)))
    if conflicts:
        state = 'RESTORE_PARTIAL_PRESERVED_UNKNOWN'
    return {'state': state, 'conflicts': sorted(set(conflicts))}


@contextmanager
def _temporary_test_scope():
    """No CLI fault flag; capability is valid only inside this live tempfile scope."""
    with tempfile.TemporaryDirectory(prefix='owned-skill-install-test-') as name:
        root = Path(name).resolve()
        token = object()
        _TEST_ROOTS[token] = root
        try:
            yield root, token
        finally:
            _TEST_ROOTS.pop(token, None)


def apply(plan_path, *, _test_token=None, _fail_after_replace=None, _unknown_test_path=None):
    p = load_plan(plan_path)
    source, target, backup = paths(p['source_root'], p['target_root'], p['backup_root'])
    if _fail_after_replace is not None or _unknown_test_path is not None:
        root = _TEST_ROOTS.get(_test_token)
        need(root is not None and all(x.is_relative_to(root) for x in (source, target, backup)), 'fault_only_live_tempfile_test_roots')
        need('.codex' not in target.parts and type(_fail_after_replace) is int and _fail_after_replace > 0, 'fault_forbidden_for_real_install')
    need(not (backup / 'WAL.jsonl').exists(), 'WAL_exists_no_success_or_failure_replay')
    need(source_tree(source, p['skills']) == (p['source_files'], p['source_dirs']), 'source_changed_since_plan')
    need(target_dirs(target, p['source_dirs']) == p['target_dirs_before'], 'target_directory_before_changed')
    need(all(file_state(target / row['path']) == row['before'] for row in p['files']), 'target_file_before_changed')
    unplanned(target, p['source_files'], p['source_dirs'], p['skills'])
    # Every independent snapshot and directory entry is durable before the first target write.
    for file in backup.iterdir():
        if file.is_file():
            with file.open('rb') as f: os.fsync(f.fileno())
    sync_dir(backup); sync_dir(backup.parent)
    new_file(backup / 'WAL.jsonl', b'')
    record(backup, 'APPLY_BEGIN', plan_sha256=digest(Path(plan_path).read_bytes()))
    changed = 0
    created = []
    intents = []
    try:
        for rel, before in p['target_dirs_before'].items():
            path = target / rel
            if not before['exists']:
                canonical(path)
                permissions = p['source_dirs'].get(rel, 0o755)
                path.mkdir(mode=permissions)
                os.chmod(path, permissions)
                item = {'event':'CREATED_DIRECTORY', 'path':rel, **directory(path)}
                created.append(item)
                record(backup, 'CREATED_DIRECTORY', path=rel, **directory(path))
                sync_dir(path.parent)
        for number, row in enumerate(p['files']):
            if row['action'] == 'noop':
                continue
            data, observed = read_file(source / row['path'])
            need(observed == row['source'], 'source_drift_during_apply')
            need(file_state(target / row['path']) == row['before'], 'target_drift_during_apply')
            path = target / row['path']
            need(parent_domain_matches(p, row['path'], created), 'target_parent_identity_changed')
            temp = path.with_name('.' + path.name + '.install-' + p['id'])
            intent = {'event':'FILE_INTENT', 'path':row['path'], 'temporary':temp.relative_to(target).as_posix()}
            intents.append(intent)
            record(backup, 'FILE_INTENT', path=row['path'], temporary=intent['temporary'])
            def staged():
                record(backup, 'FILE_STAGED', path=row['path'], temporary=intent['temporary'],
                       owned_dev=intent['owned_dev'], owned_ino=intent['owned_ino'])
                intent['ready'] = True
            atomic_bytes(path, data, row['source']['mode'], temp, row['before'], ownership=intent, ready=staged)
            changed += 1
            if changed == _fail_after_replace:
                if _unknown_test_path:
                    rel = relative(_unknown_test_path)
                    unknown = target / rel
                    need(str(rel) not in p['source_files'] and unknown.parent.is_dir(), 'unknown_test_path_scope')
                    new_file(unknown, b'concurrent user note\n', 0o601)
                raise InstallError('injected_after_replace_before_WAL_file_done')
            record(backup, 'FILE_APPLIED', path=row['path'])
        need(source_tree(source, p['skills']) == (p['source_files'], p['source_dirs']), 'source_drift_before_commit')
        need(all(known(target / row['path'], row['source']) for row in p['files']), 'installed_content_or_mode_readback_failed')
        record(backup, 'APPLY_COMMITTED', installed_files=len(p['files']), changed_files=changed)
        return {'state': 'APPLY_COMMITTED', 'files': len(p['files']), 'changed_files': changed}
    except BaseException as error:
        try:
            record(backup, 'APPLY_FAILED', error_type=type(error).__name__, safe_reason=str(error) if isinstance(error, InstallError) else 'filesystem_error')
        except OSError:
            pass
        result = restore(p, _created=created, _intents=intents)
        raise InstallError('apply_failed_' + result['state']) from error


def recover(plan_path):
    p = load_plan(plan_path)
    history = events(Path(p['backup_root']))
    need(not any(x['event'] == 'APPLY_COMMITTED' for x in history), 'successful_install_restore_is_separate_not_automatic')
    need(not any(x['event'] in ('RESTORED_BEFORE', 'RESTORE_PARTIAL_PRESERVED_UNKNOWN') for x in history), 'prior_restore_preserved_do_not_replay')
    return restore(p)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--restore', action='store_true', help='Explicit recovery of a failed/interrupted transaction only')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1] / 'skills')
    parser.add_argument('--target-root', type=Path)
    parser.add_argument('--backup-root', type=Path)
    parser.add_argument('--skill', action='append', choices=SKILLS, help='Only gated-delivery is accepted; omitted selects it for prepare')
    args = parser.parse_args()
    if args.apply or args.restore:
        need(args.plan is not None and args.target_root is None and args.backup_root is None and args.skill is None, 'apply_restore_use_frozen_plan_only')
        result = apply(args.plan) if args.apply else recover(args.plan)
    else:
        need(args.target_root is not None and args.backup_root is not None and args.plan is None, 'prepare_requires_target_and_new_backup_root')
        result = prepare(args.source_root, args.target_root, args.backup_root, skills=SKILLS if args.skill is None else args.skill)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (InstallError, OSError, ValueError) as error:
        raise SystemExit(str(error))
