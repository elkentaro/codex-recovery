import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('recovery_install', Path(__file__).with_name('install.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)/'home with spaces'
        self.home.mkdir()

    def install(self, **kwargs):
        with patch.object(m.subprocess, 'run') as run, redirect_stdout(io.StringIO()):
            m.install(self.home, **kwargs)
        return run

    def config(self):
        return self.home/'.config/session-recovery/config.json'

    def test_installs_working_launcher_and_private_external_configuration(self):
        run = self.install()
        launcher = self.home/'.local/bin/codex-recover'
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.config().stat().st_mode & 0o777, 0o600)
        config = json.loads(self.config().read_text())
        self.assertEqual(config['log_dir'], str(self.home/'codex-log'))
        self.assertIn(str(m.ROOT/'checkpoints.py'), launcher.read_text())
        unit = (self.home/'.config/systemd/user/codex-checkpoints.service').read_text()
        self.assertIn(str(m.ROOT/'checkpoints.py'), unit)
        self.assertIn('WorkingDirectory=' + str(m.ROOT) + '\n', unit)
        self.assertIn(str(self.config()), unit)
        self.assertNotIn('@COMMAND@', unit)
        agents = (self.home/'.codex/AGENTS.md').read_text()
        self.assertIn(str(self.config()), agents)
        self.assertEqual(run.call_args_list[-1].args[0], ['systemctl', '--user', 'daemon-reload'])
        result = subprocess.run([str(launcher), '--list'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No valid saved checkpoints', result.stdout)

    def test_rerun_preserves_config_schedule_and_unrelated_instructions(self):
        agents = self.home/'.codex/AGENTS.md'
        m.write(agents, 'Unrelated user instructions.\n')
        self.install(timezone_name='UTC', request_time='01:01:00', verify_time='01:15:00')
        c = json.loads(self.config().read_text())
        c.update(enabled=False, log_dir=str(self.home/'custom-log'))
        self.config().write_text(json.dumps(c))
        self.install()
        self.assertEqual(json.loads(self.config().read_text()), c)
        self.assertTrue(agents.read_text().startswith('Unrelated user instructions.'))
        self.assertEqual(agents.read_text().count(m.BEGIN), 1)
        timer = self.home/'.config/systemd/user/codex-checkpoints.timer'
        self.assertIn('01:01:00 UTC', timer.read_text())
        self.assertEqual(len(list(agents.parent.glob('AGENTS.md.before-checkpoints-*'))), 1)

    def test_import_preserves_host_paths_and_refuses_existing_destination(self):
        source = self.home/'old.json'
        c = dict(enabled=True, log_dir=str(self.home/'original-log'),
                 staging_dir=str(self.home/'original-staging'), socket=str(self.home/'original.sock'))
        source.write_text(json.dumps(c))
        self.install(import_config=source)
        migrated = json.loads(self.config().read_text())
        self.assertTrue(all(migrated[k] == v for k, v in c.items()))
        self.assertEqual(json.loads(source.read_text()), c)
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.install(import_config=source)

    def test_enable_is_explicit_and_changed_units_are_backed_up(self):
        self.install()
        unit = self.home/'.config/systemd/user/codex-checkpoints.timer'
        old = unit.read_text()
        run = self.install(enable=True, request_time='01:00:00')
        backups = list(unit.parent.glob(unit.name+'.before-session-recovery-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), old)
        self.assertEqual(run.call_args_list[-1].args[0], ['systemctl', '--user', 'enable', '--now',
                         'codex-checkpoints.timer', 'codex-checkpoints-verify.timer', 'codex-project-memory.timer'])

    def test_memory_only_enable_keeps_nightly_timer_state_and_installs_command(self):
        run = self.install(enable_memory=True)
        self.assertEqual(run.call_args_list[-1].args[0],
                         ['systemctl', '--user', 'enable', '--now', 'codex-project-memory.timer'])
        command = self.home/'.local/bin/codex-memory'
        self.assertEqual(command.stat().st_mode & 0o777, 0o700)
        result = subprocess.run([str(command), 'recent', '--cwd', str(self.home)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'not-indexed')

    def test_invalid_schedule_and_verifier_failure_write_nothing(self):
        with self.assertRaises(ValueError):
            self.install(request_time='25:00:00')
        self.assertFalse(self.config().exists())
        with patch.object(m.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'verify')):
            with self.assertRaises(subprocess.CalledProcessError):
                m.install(self.home)
        self.assertFalse(self.config().exists())

    def test_unrelated_command_is_not_replaced(self):
        launcher = self.home/'.local/bin/codex-recover'
        m.write(launcher, 'another command')
        with self.assertRaisesRegex(RuntimeError, 'unrelated command'):
            self.install()
        self.assertEqual(launcher.read_text(), 'another command')
        self.assertFalse(self.config().exists())

    def test_unit_paths_escape_percent_dollar_quotes_and_backslash(self):
        with patch.object(m, 'ROOT', m.ROOT):
            units = m.render_units(Path('/tmp/path % $ " \\/config.json'), 'UTC', '01:00:00', '01:30:00')
        unit = units['codex-checkpoints.service']
        self.assertIn('path %% $$ \\" \\\\/config.json', unit)


if __name__ == '__main__':
    unittest.main()
