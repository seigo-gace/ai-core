#!/usr/bin/env python3
import argparse, json, subprocess, sys
from pathlib import Path
from dockerutil import docker_prefix


def die(msg, code=1):
    print('BACKEND_OVERRIDE_APPLY=FAIL|' + msg)
    raise SystemExit(code)


def base_files(row):
    raw = row.get('base_config_files')
    if isinstance(raw, list):
        files = [str(x).strip() for x in raw if str(x).strip()]
    else:
        files = [x.strip() for x in str(raw or '').split(',') if x.strip()]
    if not files:
        die('no_base_config_files')
    return files


def compose_args(prefix, row, include_override=True):
    args = list(prefix) + ['compose', '-p', row['project']]
    for f in base_files(row):
        args += ['-f', f]
    if include_override:
        args += ['-f', row['override_file']]
    return args


def call(cmd, cwd):
    print('CMD=' + json.dumps(cmd))
    return subprocess.run(cmd, cwd=cwd, text=True).returncode


def validate_all(prefix, rows):
    for row in rows:
        cwd = row['working_dir']
        if not Path(cwd).is_dir():
            die('working_dir_missing:' + cwd)
        for f in base_files(row) + [row['override_file']]:
            if not Path(f).is_file():
                die('compose_file_missing:' + f)
        if call(compose_args(prefix, row, True) + ['config', '--quiet'], cwd):
            die('compose_config_invalid:' + row['project'])
    print('BACKEND_OVERRIDE_VALIDATE=PASS')


def rollback_rows(prefix, rows):
    ok = True
    for row in reversed(rows):
        cwd = row['working_dir']
        cmd = compose_args(prefix, row, False) + ['up', '-d', '--no-deps', *row['services']]
        if call(cmd, cwd):
            ok = False
            print('BACKEND_OVERRIDE_ROLLBACK_ITEM=FAIL|' + row['project'], file=sys.stderr)
        else:
            print('BACKEND_OVERRIDE_ROLLBACK_ITEM=PASS|' + row['project'])
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--validate-only', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    rows = json.loads(Path(a.manifest).read_text())
    if not rows:
        die('empty_manifest')
    prefix = docker_prefix()
    validate_all(prefix, rows)
    if a.validate_only:
        return
    if a.rollback:
        if not rollback_rows(prefix, rows):
            die('rollback_incomplete')
        print('BACKEND_OVERRIDE_ROLLBACK=PASS')
        return

    applied = []
    for row in rows:
        cwd = row['working_dir']
        cmd = compose_args(prefix, row, True) + ['up', '-d', '--no-deps', *row['services']]
        if call(cmd, cwd):
            print('BACKEND_OVERRIDE_APPLY_ITEM=FAIL|' + row['project'], file=sys.stderr)
            if applied and not rollback_rows(prefix, applied):
                die('apply_failed_and_rollback_incomplete')
            die('apply_failed_rolled_back:' + row['project'])
        applied.append(row)
        print('BACKEND_OVERRIDE_APPLY_ITEM=PASS|' + row['project'])
    print('BACKEND_OVERRIDE_APPLY=PASS')


if __name__ == '__main__':
    main()
