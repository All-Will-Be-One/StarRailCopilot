#!/usr/bin/env python3
"""Deploy a published fork master SHA on a Linux host, retaining the old checkout."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

REPOSITORY = 'https://github.com/All-Will-Be-One/StarRailCopilot'


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def service(action, name):
    subprocess.run(['systemctl', '--user', action, name], check=True)


def idle(root, service_name):
    if (root/'config/reloadalas').exists():
        raise RuntimeError('Update/restart marker exists; defer deployment')
    import yaml
    config = yaml.safe_load((root/'config/deploy.yaml').read_text())
    if config['Deploy']['Webui'].get('Run'):
        raise RuntimeError('Auto-run profiles configured; deployment would start game tasks')
    main = int(run('systemctl', '--user', 'show', service_name, '-p', 'MainPID', '--value'))
    # gui.py parent, web process and multiprocessing manager are the three idle processes.
    parents = {}
    for p in Path('/proc').glob('[0-9]*/stat'):
        try:
            parents[int(p.parent.name)] = int(p.read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            pass
    descendants = {main} if main else set()
    while True:
        added = {pid for pid, parent in parents.items() if parent in descendants}
        if added <= descendants:
            break
        descendants |= added
    if len(descendants) > 3:
        raise RuntimeError(f'SRC has {len(descendants)} processes; defer until scheduler is stopped')


def healthy(root, name):
    import yaml
    port = yaml.safe_load((root/'config/deploy.yaml').read_text())['Deploy']['Webui']['WebuiPort']
    for _ in range(30):
        try:
            active = run('systemctl', '--user', 'is-active', name) == 'active'
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=2) as response:
                if active and response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('Service or HTTP health check failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sha', help='Full published master SHA')
    parser.add_argument('--root', type=Path, default=Path.home()/'automation/StarRailCopilot')
    parser.add_argument('--service', default='starrailcopilot')
    parser.add_argument('--enable-maintenance', action='store_true', help='Enable requested game updates and src profile reminders on initial migration')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.sha):
        parser.error('a full SHA is required')
    root = args.root.resolve()
    idle(root, args.service)
    before = run('git', 'rev-parse', 'HEAD', cwd=root)
    if before == args.sha and run('git', 'remote', 'get-url', 'origin', cwd=root).removesuffix('.git') == REPOSITORY:
        healthy(root, args.service)
        print(json.dumps(dict(result='already-current', sha=before)))
        return
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stage = root.with_name(root.name+'.next-'+stamp)
    backup = root.with_name(root.name+'.previous-'+stamp)
    subprocess.run(['git', 'clone', '--single-branch', '--branch', 'master', REPOSITORY, str(stage)], check=True)
    if run('git', 'rev-parse', 'HEAD', cwd=stage) != args.sha:
        raise RuntimeError('Published master changed; inspect and deploy its current tested SHA')
    # Preserve runtime/user files; retain versioned schema and template files from the new code.
    def copy_config():
        subprocess.run(['rsync', '-a', '--exclude=template*.json', '--exclude=deploy.*.yaml',
                        '--exclude=maintenance.status.json', str(root/'config')+'/', str(stage/'config')+'/'], check=True)
    copy_config()
    python = sys.executable
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([python, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd=stage, env=env, check=True)
    idle(root, args.service)
    service('stop', args.service)
    switched = False
    try:
        copy_config()
        p = stage/'config/deploy.yaml'
        text = p.read_text()
        text = re.sub(r'(?m)^(\s+Repository:) .+$', r'\1 '+REPOSITORY, text)
        text = re.sub(r'(?m)^(\s+Branch:) .+$', r'\1 master', text)
        p.write_text(text)
        if args.enable_maintenance:
            p = stage/'config/src.json'
            data = json.loads(p.read_text())
            data['Alas']['Emulator'].update(AutoUpdateGame=True, GameUpdateCheckMinutes=360)
            data['Alas']['Error'].update(NotRunningAlert=True, NotRunningGraceMinutes=30,
                                        NotRunningRepeatMinutes=1440, NotRunningTasks='')
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
        # Keep historical logs available to the replacement and preserve the old directory.
        if (root/'log').exists():
            if (stage/'log').exists():
                shutil.rmtree(stage/'log')  # Only this candidate's offline-test logs.
            (stage/'log').symlink_to(backup/'log', target_is_directory=True)
        root.rename(backup)
        try:
            stage.rename(root)
        except Exception:
            backup.rename(root)
            raise
        switched = True
        service('start', args.service)
        healthy(root, args.service)
        actual = run('git', 'rev-parse', 'HEAD', cwd=root)
        if actual != args.sha:
            raise RuntimeError('Post-deployment SHA changed unexpectedly')
    except Exception:
        if switched:
            service('stop', args.service)
            root.rename(stage)
            backup.rename(root)
        service('start', args.service)
        raise
    print(json.dumps(dict(result='deployed', before=before, sha=actual, backup=str(backup),
                          root=str(root), service=args.service, health='HTTP 200')))


if __name__ == '__main__':
    main()
