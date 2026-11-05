"""Meaningful install/rollback fixtures. No real skill directory or external service."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install_skills.py'
spec = importlib.util.spec_from_file_location('owned_skill_installer', SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def snapshot(root):
    if not root.exists():
        return None
    result = {}
    for p in [root, *sorted(root.rglob('*'))]:
        rel = str(p.relative_to(root))
        result[rel] = ('dir', stat.S_IMODE(p.stat().st_mode)) if p.is_dir() else (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
    return result


def fixture(root, existing=True):
    source = root / 'repo' / 'skills'
    for name in installer.SKILLS:
        d = source / name
        (d / 'references' / 'nested').mkdir(parents=True)
        (d / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: fixture\n---\nnew body\n')
        os.chmod(d / 'SKILL.md', 0o640)
        (d / 'references' / 'nested' / 'policy.txt').write_bytes(b'new reference\n')
    target = root / 'installed'
    if existing:
        (target / 'gated-delivery').mkdir(parents=True)
        os.chmod(target, 0o750)
        old = target / 'gated-delivery' / 'SKILL.md'
        old.write_bytes(b'original installed skill\n')
        os.chmod(old, 0o751)
        os.utime(old, ns=(1500000000000000000, 1500000000000000000))
        unknown = target / 'gated-delivery' / 'user-note.txt'
        unknown.write_bytes(b'keep this private note\n')
        os.chmod(unknown, 0o601)
        (target / 'unmanaged').mkdir()
        (target / 'unmanaged' / 'SKILL.md').write_bytes(b'not in the owned payload\n')
    (root / 'bundles').mkdir()
    backup = root / 'bundles' / 'install-1'
    return source, target, backup


class InstallTests(unittest.TestCase):
    def test_prepare_is_read_only_and_backup_is_independent_verified_input(self):
        with installer._temporary_test_scope() as (root, token):
            source, target, backup = fixture(root)
            before = snapshot(target)
            result = installer.prepare(source, target, backup)
            self.assertEqual(result['state'], 'PREPARED_DRY_RUN_TARGET_UNCHANGED')
            self.assertEqual(snapshot(target), before)
            plan = installer.load_plan(backup / 'plan.json')
            self.assertEqual(plan['skills'], list(installer.SKILLS))
            self.assertIn('gated-delivery/user-note.txt', plan['unplanned_target_paths_reported_preserved'])
            old = next(x for x in plan['files'] if x['before']['exists'])
            copy = backup / old['before_snapshot']
            self.assertEqual(copy.read_bytes(), b'original installed skill\n')
            self.assertNotEqual(copy.stat().st_ino, (target / old['path']).stat().st_ino)
            copy.write_bytes(b'corrupted independent backup')
            with self.assertRaisesRegex(installer.InstallError, 'snapshot_changed'):
                installer.apply(backup / 'plan.json')
            self.assertEqual(snapshot(target), before)
            self.assertFalse((backup / 'WAL.jsonl').exists())

    def test_full_apply_preserves_unknown_bytes_modes_and_refuses_success_replay(self):
        with installer._temporary_test_scope() as (root, token):
            source, target, backup = fixture(root)
            unknown_before = snapshot(target)['gated-delivery/user-note.txt']
            root_mode = stat.S_IMODE(target.stat().st_mode)
            installer.prepare(source, target, backup)
            result = installer.apply(backup / 'plan.json')
            self.assertEqual(result['state'], 'APPLY_COMMITTED')
            plan = installer.load_plan(backup / 'plan.json')
            for row in plan['files']:
                self.assertTrue(installer.known(target / row['path'], row['source']))
            self.assertEqual(snapshot(target)['gated-delivery/user-note.txt'], unknown_before)
            self.assertEqual((target / 'unmanaged/SKILL.md').read_bytes(), b'not in the owned payload\n')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), root_mode)
            wal = (backup / 'WAL.jsonl').read_bytes()
            installed = snapshot(target)
            with self.assertRaisesRegex(installer.InstallError, 'WAL_exists'):
                installer.apply(backup / 'plan.json')
            with self.assertRaisesRegex(installer.InstallError, 'successful_install_restore'):
                installer.recover(backup / 'plan.json')
            self.assertEqual(snapshot(target), installed)
            self.assertEqual((backup / 'WAL.jsonl').read_bytes(), wal)

    def test_last_replace_fault_and_last_WAL_failure_restore_actual_before_shape(self):
        for existing, journal_fault in ((False, False), (True, False), (True, True)):
            with self.subTest(existing=existing, journal_fault=journal_fault):
                with installer._temporary_test_scope() as (root, token):
                    source, target, backup = fixture(root, existing)
                    before = snapshot(target)
                    installer.prepare(source, target, backup)
                    plan = installer.load_plan(backup / 'plan.json')
                    count = sum(x['action'] == 'install' for x in plan['files'])
                    if journal_fault:
                        original = installer.record
                        observed = []
                        def failing_record(bundle, event, **facts):
                            if event == 'FILE_APPLIED':
                                observed.append(event)
                                if len(observed) == count:
                                    raise OSError('injected last WAL write failure')
                            return original(bundle, event, **facts)
                        with patch.object(installer, 'record', failing_record):
                            with self.assertRaisesRegex(installer.InstallError, 'apply_failed_RESTORED_BEFORE'):
                                installer.apply(backup / 'plan.json')
                    else:
                        with self.assertRaisesRegex(installer.InstallError, 'apply_failed_RESTORED_BEFORE'):
                            installer.apply(backup / 'plan.json', _test_token=token, _fail_after_replace=count)
                    self.assertEqual(snapshot(target), before)
                    history = installer.events(backup)
                    self.assertTrue(any(x['event'] == 'APPLY_FAILED' for x in history))
                    self.assertEqual(history[-1]['event'], 'RESTORED_BEFORE')
                    self.assertEqual(installer.load_plan(backup / 'plan.json')['id'], plan['id'])
                    with self.assertRaisesRegex(installer.InstallError, 'WAL_exists'):
                        installer.apply(backup / 'plan.json')

    def test_unknown_file_in_new_directory_is_preserved_and_reports_partial_restore(self):
        with installer._temporary_test_scope() as (root, token):
            source, target, backup = fixture(root, False)
            installer.prepare(source, target, backup)
            plan = installer.load_plan(backup / 'plan.json')
            with self.assertRaisesRegex(installer.InstallError, 'RESTORE_PARTIAL_PRESERVED_UNKNOWN'):
                installer.apply(backup / 'plan.json', _test_token=token,
                                _fail_after_replace=len(plan['files']),
                                _unknown_test_path='gated-delivery/references/nested/user-note.txt')
            user = target / 'gated-delivery/references/nested/user-note.txt'
            self.assertEqual(user.read_bytes(), b'concurrent user note\n')
            self.assertEqual(stat.S_IMODE(user.stat().st_mode), 0o601)
            self.assertTrue(all(not (target / row['path']).exists() for row in plan['files']))
            self.assertEqual(installer.events(backup)[-1]['event'], 'RESTORE_PARTIAL_PRESERVED_UNKNOWN')

    def test_matching_after_from_concurrent_actor_is_preserved_without_ownership(self):
        for replaced in (False, True):
            with self.subTest(concurrent_replaces_our_inode=replaced):
                with installer._temporary_test_scope() as (root, token):
                    source, target, backup = fixture(root)
                    installer.prepare(source, target, backup)
                    original = installer.atomic_bytes
                    concurrent = {}
                    def fail_with_concurrent_after(path, raw, permissions, temp, before_state, **kwargs):
                        original(path, raw, permissions, temp, before_state, **kwargs)
                        if concurrent:
                            return  # Restoration must not inject a second concurrent mutation.
                        other = path if replaced else target / 'gated-delivery/references/nested/policy.txt'
                        incoming = other.with_name('.concurrent-new-inode')
                        expected = (source / other.relative_to(target)).read_bytes()
                        installer.new_file(incoming, expected, installer.mode(source / other.relative_to(target)))
                        os.replace(incoming, other)
                        concurrent.update(path=other, state=installer.file_state(other))
                        raise OSError('injected after concurrent matching-content file')
                    with patch.object(installer, 'atomic_bytes', fail_with_concurrent_after):
                        with self.assertRaisesRegex(installer.InstallError, 'RESTORE_PARTIAL_PRESERVED_UNKNOWN'):
                            installer.apply(backup / 'plan.json')
                    self.assertEqual(installer.file_state(concurrent['path']), concurrent['state'])
                    if not replaced:
                        self.assertEqual((target / 'gated-delivery/SKILL.md').read_bytes(), b'original installed skill\n')
                    self.assertEqual((target / 'gated-delivery/user-note.txt').read_bytes(), b'keep this private note\n')
                    self.assertEqual(installer.mode(target / 'gated-delivery/user-note.txt'), 0o601)
                    history = installer.events(backup)
                    self.assertEqual(history[-1]['event'], 'RESTORE_PARTIAL_PRESERVED_UNKNOWN')
                    self.assertIn(str(concurrent['path'].relative_to(target)), history[-1]['conflicts'])

    def test_explicit_interrupted_recovery_uses_durable_staged_inode_before_done_event(self):
        with installer._temporary_test_scope() as (root, token):
            source, target, backup = fixture(root)
            before = snapshot(target)
            installer.prepare(source, target, backup)
            plan = installer.load_plan(backup / 'plan.json')
            count = sum(x['action'] == 'install' for x in plan['files'])
            # Preserve a process-exit shape: last replacement happened, automatic restore did not run.
            with patch.object(installer, 'restore', return_value={'state': 'SIMULATED_PROCESS_EXIT'}):
                with self.assertRaisesRegex(installer.InstallError, 'SIMULATED_PROCESS_EXIT'):
                    installer.apply(backup / 'plan.json', _test_token=token, _fail_after_replace=count)
            history = installer.events(backup)
            self.assertEqual(sum(x['event'] == 'FILE_STAGED' for x in history), count)
            self.assertEqual(sum(x['event'] == 'FILE_APPLIED' for x in history), count - 1)
            self.assertEqual(installer.recover(backup / 'plan.json')['state'], 'RESTORED_BEFORE')
            self.assertEqual(snapshot(target), before)
            self.assertEqual((target / 'gated-delivery/SKILL.md').stat().st_mtime_ns, 1500000000000000000)

    def test_repository_owned_payload_full_and_last_failure_in_temporary_roots(self):
        actual_source = SCRIPT.parents[1] / 'skills'
        actual_files, actual_dirs = installer.source_tree(actual_source)
        for last_fault in (False, True):
            with self.subTest(last_fault=last_fault):
                with installer._temporary_test_scope() as (root, token):
                    source, target, backup = fixture(root)
                    shutil.rmtree(source)  # Only this test-created source inside the live tempfile.
                    source.mkdir()
                    for name in installer.SKILLS:
                        shutil.copytree(actual_source / name, source / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', '.DS_Store', '.git'))
                    before = snapshot(target)
                    installer.prepare(source, target, backup)
                    plan = installer.load_plan(backup / 'plan.json')
                    self.assertEqual(set(plan['source_files']), set(actual_files))
                    self.assertEqual(plan['source_dirs'], actual_dirs)
                    for rel, info in actual_files.items():
                        self.assertEqual({k: plan['source_files'][rel][k] for k in ('bytes','sha256','mode')}, {k:info[k] for k in ('bytes','sha256','mode')})
                    if last_fault:
                        count = sum(x['action'] == 'install' for x in plan['files'])
                        with self.assertRaisesRegex(installer.InstallError, 'apply_failed_RESTORED_BEFORE'):
                            installer.apply(backup / 'plan.json', _test_token=token, _fail_after_replace=count)
                        self.assertEqual(snapshot(target), before)
                        self.assertEqual(installer.events(backup)[-1]['event'], 'RESTORED_BEFORE')
                    else:
                        self.assertEqual(installer.apply(backup / 'plan.json')['state'], 'APPLY_COMMITTED')
                        for row in plan['files']:
                            self.assertTrue(installer.known(target / row['path'], row['source']))
                        self.assertEqual(snapshot(target)['gated-delivery/user-note.txt'], before['gated-delivery/user-note.txt'])
                        self.assertEqual(snapshot(target)['unmanaged/SKILL.md'], before['unmanaged/SKILL.md'])

    def test_owned_install_preserves_project_domain_aliases_and_last_restore(self):
        research = ('project-analysis', 'project-retrospective')
        for last_fault in (False, True):
            with self.subTest(research_last_fault=last_fault):
                with installer._temporary_test_scope() as (root, token):
                    source, target, backup = fixture(root)
                    canonical = root / 'workspace' / 'skills'
                    for name in research:
                        (canonical / name).mkdir(parents=True)
                        old = canonical / name / 'SKILL.md'
                        old.write_bytes(('old canonical ' + name + '\n').encode())
                        os.chmod(old, 0o641)
                        (target / name).symlink_to(canonical / name, target_is_directory=True)
                    links = {name: ((target / name).lstat().st_ino, os.readlink(target / name)) for name in research}
                    before_canonical = snapshot(canonical)
                    before_target = snapshot(target)
                    installer.prepare(source, target, backup)
                    selected = installer.load_plan(backup / 'plan.json')
                    self.assertEqual(selected['skills'], ['gated-delivery'])
                    self.assertTrue(all(x['path'].startswith('gated-delivery/') for x in selected['files']))
                    if last_fault:
                        count = sum(x['action'] == 'install' for x in selected['files'])
                        with self.assertRaisesRegex(installer.InstallError, 'apply_failed_RESTORED_BEFORE'):
                            installer.apply(backup / 'plan.json', _test_token=token, _fail_after_replace=count)
                        self.assertEqual(snapshot(target), before_target)
                        self.assertEqual(installer.events(backup)[-1]['event'], 'RESTORED_BEFORE')
                    else:
                        self.assertEqual(installer.apply(backup / 'plan.json')['state'], 'APPLY_COMMITTED')
                        self.assertTrue(all(installer.known(target / row['path'], row['source']) for row in selected['files']))
                    self.assertEqual(snapshot(canonical), before_canonical)
                    self.assertEqual({name: ((target / name).lstat().st_ino, os.readlink(target / name)) for name in research}, links)
                    other = root / 'bundles' / 'research-batch'
                    with self.assertRaisesRegex(installer.InstallError, 'nonempty_owned_skill_subset_required'):
                        installer.prepare(source, canonical, other, skills=list(reversed(research)))
                    self.assertFalse(other.exists())
                    self.assertEqual(snapshot(canonical), before_canonical)
                    self.assertFalse((canonical / 'gated-delivery').exists())
                    self.assertEqual({name: ((target / name).lstat().st_ino, os.readlink(target / name)) for name in research}, links)

    def test_source_target_drift_and_aliases_refuse_before_first_target_write(self):
        for fault in ('source', 'target', 'alias', 'symlink', 'traversal'):
            with self.subTest(fault=fault):
                with installer._temporary_test_scope() as (root, token):
                    source, target, backup = fixture(root)
                    if fault in ('source', 'target'):
                        installer.prepare(source, target, backup)
                        altered = source / 'gated-delivery/references/nested/policy.txt' if fault == 'source' else target / 'gated-delivery/SKILL.md'
                        altered.write_bytes(b'changed after planning\n')
                        current = snapshot(target)
                        with self.assertRaises(installer.InstallError):
                            installer.apply(backup / 'plan.json')
                        self.assertEqual(snapshot(target), current)
                        self.assertFalse((backup / 'WAL.jsonl').exists())
                    else:
                        before = snapshot(target)
                        if fault == 'alias':
                            destination = source
                        elif fault == 'symlink':
                            destination = root / 'alias-target'; destination.symlink_to(target, target_is_directory=True)
                        else:
                            destination = target / '..' / 'installed'
                        with self.assertRaises(installer.InstallError):
                            installer.prepare(source, destination, backup)
                        self.assertEqual(snapshot(target), before)
                        self.assertFalse(backup.exists())

    def test_fault_capability_has_no_real_install_or_unregistered_path(self):
        with installer._temporary_test_scope() as (root, token):
            source, target, backup = fixture(root, False)
            installer.prepare(source, target, backup)
            with self.assertRaisesRegex(installer.InstallError, 'fault_only_live_tempfile'):
                installer.apply(backup / 'plan.json', _fail_after_replace=1)
            self.assertFalse(target.exists())
            self.assertFalse((backup / 'WAL.jsonl').exists())
            # A fake .codex inside the tempfile is also conservatively denied; no real home is used.
            fake_home = root / '.codex'; fake_home.mkdir()
            target2 = fake_home / 'skills'
            backup2 = root / 'bundles' / 'install-2'
            installer.prepare(source, target2, backup2)
            with self.assertRaisesRegex(installer.InstallError, 'fault_forbidden_for_real_install'):
                installer.apply(backup2 / 'plan.json', _test_token=token, _fail_after_replace=1)
            self.assertFalse(target2.exists())

    def test_former_domain_selection_refuses_before_bundle_or_target_write(self):
        for selection in (['project-analysis'], ['project-retrospective'],
                          ['gated-delivery', 'project-analysis']):
            with self.subTest(selection=selection):
                with installer._temporary_test_scope() as (root, token):
                    source, target, backup = fixture(root)
                    before = snapshot(target)
                    with self.assertRaisesRegex(installer.InstallError, 'nonempty_owned_skill_subset_required'):
                        installer.prepare(source, target, backup, skills=selection)
                    self.assertEqual(snapshot(target), before)
                    self.assertFalse(backup.exists())


if __name__ == '__main__':
    unittest.main()
