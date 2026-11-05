import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/gated-delivery/scripts'
sys.path.insert(0, str(SCRIPTS))
from delivery_common import evaluate, json_read, load_plan, digest
from run_check import run

@unittest.skipUnless(os.name == 'posix', 'runner process groups require POSIX')
class EvidenceGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'app.py').write_text('def answer(): return 0\n')
        (self.root / 'test_app.py').write_text('import unittest\nfrom app import answer\nclass Contract(unittest.TestCase):\n def test_answer(self): self.assertEqual(answer(), 7)\n')
        self.plan = {'schema_version': 1, 'task_id': 'answer-contract', 'source_files': ['app.py', 'test_app.py'],
                     'oracle_files': ['test_app.py'], 'criteria': [{'id': 'A1', 'description': 'answer is7', 'kind': 'positive', 'level': 'unit', 'requires_red': True, 'test_ids': ['T1']}],
                     'checks': [{'id': 'T1', 'kind': 'test', 'level': 'unit', 'command': [sys.executable, '-B', '-m', 'unittest', 'test_app', '-v'], 'timeout_seconds': 5, 'evidence': 'evidence/T1', 'report': {'format': 'unittest'}}]}
        self.save()

    def save(self):
        (self.root / 'plan.json').write_text(json.dumps(self.plan))

    def execute(self):
        return run(self.root, 'plan.json', 'T1', self.plan['checks'][0]['command'])

    def green(self):
        self.execute()
        (self.root / 'app.py').write_text('def answer(): return 7\n')
        return self.execute()

    def test_actual_assertion_red_then_green_and_frozen_history(self):
        first = self.execute()
        self.assertEqual((first['returncode'], first['counts']['failures']), (1, 1))
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'REJECTED')
        (self.root / 'app.py').write_text('def answer(): return 7\n')
        last = self.execute()
        self.assertEqual(last['state'], 'passed')
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'PASSED')
        self.assertEqual(len(list((self.root / 'evidence/T1/runs').glob('*/receipt.json'))), 2)

    def test_changed_oracle_cannot_replace_original_red(self):
        self.execute()
        p = self.root / 'test_app.py'
        p.write_text(p.read_text().replace('answer(), 7', 'answer(), 0'))
        self.assertEqual(self.execute()['state'], 'passed')
        result = evaluate(self.root, 'plan.json')
        self.assertEqual(result['state'], 'REJECTED')
        self.assertTrue(any('oracle' in x for x in result['problems']))

    def test_source_change_after_green_is_stale(self):
        self.green()
        (self.root / 'app.py').write_text('def answer(): return 9\n')
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'REJECTED')

    def test_raw_output_tamper_is_rejected(self):
        result = self.green()
        (self.root / result['stderr']['path']).write_text('Ran1test OK fabricated\n')
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'REJECTED')

    def test_forged_counter_cannot_override_original_output(self):
        self.green()
        pointer_file = self.root / 'evidence/T1/latest.json'
        pointer = json_read(pointer_file)
        receipt_file = self.root / pointer['receipt']
        receipt = json_read(receipt_file)
        receipt['counts']['tests'] = 999
        receipt_file.write_text(json.dumps(receipt))
        pointer['sha256'] = digest(receipt_file)
        pointer_file.write_text(json.dumps(pointer))
        result = evaluate(self.root, 'plan.json')
        self.assertEqual(result['state'], 'REJECTED')
        self.assertTrue(any('counts differ' in x for x in result['problems']))

    def test_zero_methods_and_skips_do_not_pass(self):
        self.plan['criteria'][0]['requires_red'] = False
        self.save()
        (self.root / 'test_app.py').write_text('import unittest\n')
        self.assertEqual(self.execute()['state'], 'failed')
        (self.root / 'test_app.py').write_text('import unittest\nclass Empty(unittest.TestCase):\n @unittest.skip("not accepted")\n def test_it(self): pass\n')
        self.assertEqual(self.execute()['counts']['skipped'], 1)
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'REJECTED')

    def test_explicit_command_mismatch_does_not_execute(self):
        with self.assertRaises(ValueError): run(self.root, 'plan.json', 'T1', [sys.executable, '-c', 'raise SystemExit(0)'])
        self.assertFalse((self.root / 'evidence').exists())

    def test_missing_or_lower_evidence_cannot_cover_ui(self):
        self.plan['criteria'][0]['level'] = 'ui'; self.save()
        with self.assertRaises(ValueError): load_plan(self.root, 'plan.json')

    def test_side_effects_need_a_real_negative_contract(self):
        self.plan.update(side_effecting=True, authorized_actions=['local preview'], protected=['no send'], stop_conditions=['missing auth'])
        self.save()
        with self.assertRaises(ValueError): load_plan(self.root, 'plan.json')
        self.plan['criteria'].append({'id': 'A2', 'description': 'no send', 'kind': 'negative', 'level': 'unit', 'test_ids': ['T1']}); self.save()
        self.assertEqual(load_plan(self.root, 'plan.json')[0]['task_id'], 'answer-contract')

    def test_path_and_symlink_escape_refused_before_exec(self):
        self.plan['checks'][0]['evidence'] = '../outside'; self.save()
        with self.assertRaises(ValueError): self.execute()
        self.plan['checks'][0]['evidence'] = 'escape/T1'; self.save()
        (self.root / 'escape').symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError): self.execute()

    def test_newer_failure_is_not_hidden_by_previous_success(self):
        self.green()
        (self.root / 'app.py').write_text('def answer(): return 0\n')
        self.execute()
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'REJECTED')

    def test_rolling_latest_back_to_old_success_is_rejected(self):
        self.plan['criteria'][0]['requires_red'] = False
        self.plan['checks'][0].update(kind='command', command=[sys.executable, '-c', 'from pathlib import Path; raise SystemExit(int(Path("fail.flag").exists()))'])
        self.save(); self.execute()
        pointer = self.root / 'evidence/T1/latest.json'
        old = pointer.read_bytes()
        (self.root / 'fail.flag').touch()
        self.assertEqual(self.execute()['state'], 'failed')
        pointer.write_bytes(old)
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'REJECTED')

    def test_unfinished_new_run_blocks_old_success(self):
        self.green()
        (self.root / 'evidence/T1/runs/99991231T235959999999Z-00000000').mkdir()
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'REJECTED')

    def test_fast_output_is_bounded_and_cannot_pass(self):
        self.plan['criteria'][0]['requires_red'] = False
        self.plan['checks'][0].update(kind='command', command=[sys.executable, '-c', 'import os; os.write(1, b"x" * (3 * 1024**2))'])
        self.save(); result = self.execute()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['stop_reason'], 'check_output_limit')
        self.assertLessEqual(result['stdout']['bytes'], 1024**2)
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'REJECTED')

    def test_runs_symlink_is_rejected_without_external_write(self):
        with tempfile.TemporaryDirectory() as other:
            (self.root / 'evidence/T1').mkdir(parents=True)
            (self.root / 'evidence/T1/runs').symlink_to(Path(other), target_is_directory=True)
            with self.assertRaises(ValueError): self.execute()
            self.assertEqual(list(Path(other).iterdir()), [])

    def test_junit_link_created_by_command_is_not_archived(self):
        self.plan['criteria'][0]['requires_red'] = False
        (self.root / 'existing.xml').write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="old"/></testsuite></testsuites>')
        self.plan['checks'][0].update(command=[sys.executable, '-c', 'from pathlib import Path; Path("results.xml").symlink_to("existing.xml")'], report={'format': 'junit', 'path': 'results.xml'})
        self.save(); result = self.execute()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('symlink', result['stop_reason'])
        self.assertNotIn('report', result)
        self.assertEqual(list((self.root / 'evidence/T1/runs').glob('*/junit.xml')), [])

    def test_timeout_is_a_failed_local_check_not_product_sla(self):
        self.plan['criteria'][0]['requires_red'] = False
        self.plan['checks'][0].update(kind='command', command=[sys.executable, '-c', 'import time; time.sleep(4)'], timeout_seconds=1)
        self.save(); result = self.execute()
        self.assertEqual(result['stop_reason'], 'check_timeout')
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'REJECTED')

    def test_junit_is_archived_and_old_report_is_not_reused(self):
        self.plan['criteria'][0]['requires_red'] = False
        xml = '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="real"/></testsuite></testsuites>'
        self.plan['checks'][0].update(command=[sys.executable, '-c', 'from pathlib import Path; Path("results.xml").write_text(' + repr(xml) + ')'], report={'format': 'junit', 'path': 'results.xml'})
        self.save(); result = self.execute()
        self.assertEqual(result['state'], 'passed')
        (self.root / 'results.xml').write_text('later mutable report')
        self.assertEqual(evaluate(self.root, 'plan.json')['state'], 'PASSED')
        self.assertTrue((self.root / result['report']['path']).exists())
        self.plan['checks'][0]['command'] = [sys.executable, '-c', 'pass']; self.save()
        self.assertEqual(self.execute()['state'], 'failed')

if __name__ == '__main__': unittest.main()
