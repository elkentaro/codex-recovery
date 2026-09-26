#!/usr/bin/env python3
"""Install user-only checkpoint instructions and timers. No root commands."""
import argparse
from version import __version__
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
BEGIN = '<!-- local-codex-checkpoints:begin -->'
END = '<!-- local-codex-checkpoints:end -->'
UNITS = ('codex-checkpoints.service', 'codex-checkpoints.timer',
         'codex-checkpoints-verify.service', 'codex-checkpoints-verify.timer',
         'codex-project-memory.service', 'codex-project-memory.timer')

def write(path, text, mode=0o600):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError(f'Refusing symlink: {path}')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.checkpoint-install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def instructions(old, block):
    if BEGIN in old or END in old:
        if old.count(BEGIN) != 1 or old.count(END) != 1:
            raise RuntimeError('Malformed existing checkpoint instruction block')
        start, end = old.index(BEGIN), old.index(END)
        if end < start:
            raise RuntimeError('Malformed existing checkpoint instruction block')
        return old[:start] + BEGIN+'\n'+block.rstrip()+'\n'+END + old[end+len(END):]
    return old.rstrip() + ('\n\n' if old.strip() else '') + BEGIN+'\n'+block.rstrip()+'\n'+END+'\n'

def unit_quote(value, command=False):
    if any(ord(ch) < 32 for ch in value):
        raise ValueError('Control characters are not supported in unit paths')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'

def render_units(config_path, timezone_name, request_time, verify_time):
    ZoneInfo(timezone_name)
    for value in (request_time, verify_time):
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]', value):
            raise ValueError('Schedule times must be HH:MM:SS')
    command = ' '.join(unit_quote(str(x), command=True) for x in
                       (sys.executable, ROOT/'checkpoints.py', '--config', config_path))
    values = {'@ROOT@': str(ROOT).replace('%', '%%'), '@COMMAND@': command,
              '@TIMEZONE@': timezone_name, '@REQUEST_TIME@': request_time,
              '@VERIFY_TIME@': verify_time}
    values['@MEMORY_COMMAND@'] = ' '.join(unit_quote(str(x), command=True) for x in
                                        (sys.executable, ROOT/'project_memory.py', '--config', config_path))
    result = {}
    for name in UNITS:
        text = (ROOT/'systemd'/name).read_text()
        for key, value in values.items():
            text = text.replace(key, value)
        result[name] = text
    return result

def install(home, enable=False, import_config=None, timezone_name=None,
            request_time=None, verify_time=None, enable_memory=False):
    from websockets.sync.client import unix_connect  # Dependency preflight; no installation.
    with sqlite3.connect(':memory:') as test_db:
        test_db.execute('CREATE VIRTUAL TABLE search_preflight USING fts5(content)')
    config_path = home/'.config/session-recovery/config.json'
    if import_config and config_path.exists():
        raise RuntimeError('Destination configuration already exists; migration will not overwrite it')
    source = Path(import_config) if import_config else config_path
    c = json.loads(source.read_text()) if source.exists() else {
        'enabled': True, 'log_dir': str(home/'codex-log'),
        'staging_dir': f'/tmp/codex-checkpoints-{os.getuid()}',
        'socket': str(home/'.codex/app-server-control/app-server-control.sock')}
    if import_config and not source.is_file():
        raise RuntimeError('Import configuration does not exist')
    if not isinstance(c.get('enabled'), bool):
        raise ValueError('Configuration requires boolean enabled')
    for key in ('log_dir', 'socket'):
        if not isinstance(c.get(key), str) or not c[key]:
            raise ValueError(f'Configuration requires {key}')
    if 'staging_dir' not in c:
        c['staging_dir'] = f'/tmp/codex-checkpoints-{os.getuid()}'
    for key in ('log_dir', 'staging_dir', 'socket'):
        c[key] = str(Path(c[key]).expanduser().absolute())
    for key, value, default in (('timezone', timezone_name, 'Asia/Tokyo'),
                                ('request_time', request_time, '02:40:00'),
                                ('verify_time', verify_time, '02:55:00')):
        c[key] = value if value is not None else c.get(key, default)
    units_text = render_units(config_path, c['timezone'], c['request_time'], c['verify_time'])
    # Validate generated files before touching installed configuration or units.
    with tempfile.TemporaryDirectory(prefix='session-recovery-units-') as tmp:
        paths = []
        for name, text in units_text.items():
            candidate = Path(tmp)/name
            candidate.write_text(text)
            paths.append(str(candidate))
        subprocess.run(['systemd-analyze', '--user', 'verify', *paths], check=True)
    agents = home/'.codex'/'AGENTS.md'
    old = agents.read_text() if agents.exists() else ''
    block = (ROOT/'global-instructions.md').read_text().replace('@CONFIG_PATH@', str(config_path))
    new = instructions(old, block)
    launchers = {}
    for name, script, extra in (('codex-recover', 'checkpoints.py', ['recover']),
                                ('codex-memory', 'project_memory.py', [])):
        launcher = home/'.local/bin'/name
        text = ('#!/bin/sh\n# Generated by session-recovery/install.py\nexec ' +
                shlex.join([sys.executable, str(ROOT/script), '--config', str(config_path), *extra]) + ' "$@"\n')
        if launcher.exists() and '# Generated by session-recovery/install.py\n' not in launcher.read_text():
            raise RuntimeError(f'Refusing to replace an unrelated command: {launcher}')
        launchers[launcher] = text
    if old and old != new:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        write(agents.with_name('AGENTS.md.before-checkpoints-'+stamp), old)
    write(agents, new)
    write(config_path, json.dumps(c, indent=2)+'\n')
    log = Path(c['log_dir'])
    write(log/'.gitignore', '*\n')
    log.chmod(0o700)
    units = home/'.config'/'systemd'/'user'
    for name, text in units_text.items():
        target = units/name
        if target.exists() and target.read_text() != text:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            write(target.with_name(name+'.before-session-recovery-'+stamp), target.read_text())
        write(target, text)
    for launcher, text in launchers.items():
        write(launcher, text, mode=0o700)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    if enable:
        subprocess.run(['systemctl', '--user', 'enable', '--now',
                        'codex-checkpoints.timer', 'codex-checkpoints-verify.timer',
                        'codex-project-memory.timer'], check=True)
    elif enable_memory:
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'codex-project-memory.timer'], check=True)
    print('Installed global instructions, private configuration, user units and commands:',
          ', '.join(str(p) for p in launchers))
    print('Timers enabled.' if enable else 'Shared-memory timer enabled; nightly enablement preserved.' if enable_memory
          else 'Existing timer enablement preserved; new timers are not enabled.')
    print('No prompts sent. Existing sessions receive the instructions in their checkpoint request.')
    if str(launcher.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        print('Add this to your shell configuration, then reopen the terminal:')
        print('  export PATH="$HOME/.local/bin:$PATH"')

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', action='version', version=f'Session Recovery {__version__}')
    p.add_argument('--enable', action='store_true')
    p.add_argument('--enable-memory', action='store_true', help='enable only the local shared-memory collector timer')
    p.add_argument('--import-config', type=Path, help='migrate a private config without overwriting an installed config')
    p.add_argument('--timezone', help='IANA timezone; defaults to Asia/Tokyo on first install')
    p.add_argument('--request-time', help='HH:MM:SS; defaults to 02:40:00 on first install')
    p.add_argument('--verify-time', help='HH:MM:SS; defaults to 02:55:00 on first install')
    a = p.parse_args()
    if os.geteuid() == 0:
        p.error('Run as your ordinary Codex account, without sudo')
    install(Path.home(), a.enable, a.import_config, a.timezone, a.request_time, a.verify_time, a.enable_memory)
