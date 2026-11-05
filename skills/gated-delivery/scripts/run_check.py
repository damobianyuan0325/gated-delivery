"""Run one explicitly supplied check; preserve failures and compare final source bytes."""
from pathlib import Path
import argparse, datetime, json, os, shutil, signal, subprocess, sys, time, uuid
from delivery_common import load_plan, path_in, snapshot, digest, json_new, test_counts

OUTPUT_FILE_LIMIT = 1024**2

def output_file_limit():
    # Child-only POSIX bound, including processes that exit between polls.
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_FILE_LIMIT, OUTPUT_FILE_LIMIT))

def run(root, planfile, check_id, command):
    root = Path(root).resolve(strict=True)
    plan, plan_sha = load_plan(root, planfile)
    check = next(x for x in plan['checks'] if x['id'] == check_id)
    if command != check['command']: raise ValueError('explicit argv does not match plan; no command executed')
    if os.name != 'posix': raise ValueError('this bounded runner currently requires POSIX process groups; use native checks on other systems')
    base = path_in(root, check['evidence']); base.mkdir(parents=True, exist_ok=True)
    runs = path_in(root, check['evidence'] + '/runs'); runs.mkdir(exist_ok=True)
    folder = path_in(root, str(runs.relative_to(root) / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8])))
    folder.mkdir(exist_ok=False)
    before = snapshot(root, plan['source_files']); oracle = snapshot(root, plan.get('oracle_files', []))
    start = datetime.datetime.now(datetime.timezone.utc).isoformat(); began = time.monotonic(); reason = None
    stdout = folder / 'stdout.txt'; stderr = folder / 'stderr.txt'
    report_source = path_in(root, check['report']['path']) if check.get('report', {}).get('format') == 'junit' else None
    report_before = report_source.stat() if report_source is not None and report_source.exists() else None
    with stdout.open('xb') as out, stderr.open('xb') as err:
        p = subprocess.Popen(command, cwd=root, stdout=out, stderr=err, start_new_session=True, preexec_fn=output_file_limit)
        while p.poll() is None:
            if time.monotonic() - began > check['timeout_seconds']: reason = 'check_timeout'
            if stdout.stat().st_size + stderr.stat().st_size > 2 * 1024**2: reason = 'check_output_limit'
            if reason:
                try: os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: p.wait(timeout=2)
                except subprocess.TimeoutExpired: os.killpg(p.pid, signal.SIGKILL); p.wait()
                break
            time.sleep(.03)
        code = p.wait()
    if stdout.stat().st_size >= OUTPUT_FILE_LIMIT or stderr.stat().st_size >= OUTPUT_FILE_LIMIT:
        reason = reason or 'check_output_limit'
    # A check owns only its new POSIX process group, never unrelated services.
    try:
        os.killpg(p.pid, 0)
        os.killpg(p.pid, signal.SIGTERM)
        reason = reason or 'check_left_process_group_running'
    except ProcessLookupError:
        pass
    after = snapshot(root, plan['source_files'])
    counts = None
    archived_report = None
    try:
        if report_source is not None:
            report_source = path_in(root, check['report']['path'], exists=True)
            after_report = report_source.stat()
            if report_before and (after_report.st_ino, after_report.st_mtime_ns, after_report.st_ctime_ns) == (report_before.st_ino, report_before.st_mtime_ns, report_before.st_ctime_ns):
                raise ValueError('JUnit report was not produced by this check')
            if after_report.st_size > 5 * 1024**2: raise ValueError('JUnit report too large')
            archived_report = folder / 'junit.xml'; shutil.copyfile(report_source, archived_report)
        counts = test_counts(check, root, stdout, stderr, archived_report)
    except (ValueError, OSError, KeyError, TypeError) as e: reason = reason or 'invalid_test_report: ' + str(e)
    if before != after: reason = reason or 'source_changed_during_check'
    if counts and (not counts['tests'] or any(counts[k] for k in ('failures', 'errors', 'skipped', 'xfail'))):
        reason = reason or 'test_counts_not_clean'
    r = {'schema_version': 1, 'task_id': plan['task_id'], 'check_id': check_id, 'plan_sha256': plan_sha,
         'command': command, 'kind': check['kind'], 'level': check['level'], 'started_at': start,
         'duration_seconds': round(time.monotonic() - began, 6), 'returncode': code,
         'state': 'passed' if code == 0 and not reason else 'failed', 'stop_reason': reason,
         'source_before': before, 'source_after': after, 'oracle_snapshot': oracle, 'counts': counts}
    if archived_report is not None:
        r['report'] = {'path': str(archived_report.relative_to(root)), 'bytes': archived_report.stat().st_size, 'sha256': digest(archived_report)}
    for key, file in [('stdout', stdout), ('stderr', stderr)]:
        r[key] = {'path': str(file.relative_to(root)), 'bytes': file.stat().st_size, 'sha256': digest(file)}
    receipt = folder / 'receipt.json'; json_new(receipt, r)
    pointer = {'receipt': str(receipt.relative_to(root)), 'sha256': digest(receipt)}
    tmp = base / ('.latest-' + uuid.uuid4().hex + '.json'); json_new(tmp, pointer); os.replace(tmp, base / 'latest.json')
    return r

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__); ap.add_argument('--root', type=Path, required=True); ap.add_argument('--plan', required=True); ap.add_argument('--check', required=True); ap.add_argument('command', nargs=argparse.REMAINDER); a = ap.parse_args()
    try:
        cmd = a.command[1:] if a.command[:1] == ['--'] else a.command
        r = run(a.root, a.plan, a.check, cmd)
        print(json.dumps({k: r[k] for k in ('check_id', 'state', 'returncode', 'duration_seconds', 'counts', 'stop_reason')}, ensure_ascii=False))
        sys.exit(0 if r['state'] == 'passed' else 1)
    except (ValueError, OSError, KeyError, StopIteration) as e:
        print(json.dumps({'state': 'REJECTED', 'reason': str(e)}, ensure_ascii=False)); sys.exit(2)
