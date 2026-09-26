import importlib.util
import io
from contextlib import redirect_stdout, redirect_stderr
import json
import os
import shlex
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('checkpoints', Path(__file__).with_name('checkpoints.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
TID = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'

class FakeRpc:
    def __init__(self, root, status='active', flags=None):
        self.calls = []
        self.thread = dict(id=TID, cwd=str(root), status={'type': status, 'activeFlags': flags or []},
                           parentThreadId=None, ephemeral=False)
        self.turn = dict(id='active-turn', status='inProgress' if status == 'active' else 'completed', completedAt=time.time()-10)
        self.fail = None
    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/loaded/list':
            return dict(data=[TID], nextCursor=None)
        if method == 'thread/read':
            return dict(thread=self.thread)
        if method == 'thread/turns/list':
            return dict(data=[self.turn])
        if method in ('turn/steer', 'turn/start'):
            if self.fail:
                raise self.fail
            return {'turnId': 'active-turn', 'turn': {'id': 'checkpoint-turn'}}
        raise AssertionError(method)

class Checkpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = dict(enabled=True, log_dir=self.root/'central', staging_dir=self.root/'staging')
        self.report = self.c['log_dir']/'runs'/'today.json'
        self.rpc = FakeRpc(self.root)
    def send(self):
        with patch.object(m, 'snapshot', return_value={'note': 'fixture'}):
            return m.request(self.rpc, self.c, self.report)
    def write(self, entry, **kwargs):
        text = m.marker(entry) + '\n'
        text += '\n'.join('## '+s+'\nRecorded evidence or None.\n' for s in m.SECTIONS)
        m.atomic(entry['path'], text)
        return Path(entry['path'])
    def test_discovery_read_only(self):
        rows = m.discover(self.rpc, self.c)
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(not method.startswith('turn/') for method, _ in self.rpc.calls))
    def test_subagents_excluded(self):
        self.rpc.thread['parentThreadId'] = OTHER
        self.assertEqual(m.discover(self.rpc, self.c), [])
    def test_staging_keeps_memory_outside_public_projects(self):
        self.assertEqual(m.memory_dir(self.c, self.root/'public'), self.root/'staging')
        self.assertEqual(m.memory_dir(self.c, self.root.parent/'unrelated'), self.root/'staging')
    def test_active_steered_exact_turn_no_overrides(self):
        r = self.send()
        method, params = self.rpc.calls[-1]
        self.assertEqual(method, 'turn/steer')
        self.assertEqual(params['expectedTurnId'], 'active-turn')
        self.assertEqual(set(params), {'threadId', 'input', 'expectedTurnId'})
        self.assertEqual(r['entries'][0]['delivery'], 'accepted')
    def test_idle_uses_existing_thread(self):
        self.rpc = FakeRpc(self.root, 'idle')
        self.send()
        self.assertEqual(self.rpc.calls[-1][0], 'turn/start')
        self.assertEqual(self.rpc.calls[-1][1]['threadId'], TID)
    def test_waiting_approval_is_not_answered(self):
        self.rpc.thread['status']['activeFlags'] = ['waitingOnApproval']
        r = self.send()
        self.assertIn('left untouched', r['entries'][0]['error'])
        self.assertFalse(any(x.startswith('turn/') for x, _ in self.rpc.calls))
    def test_uncertain_delivery_does_not_retry(self):
        self.rpc.fail = TimeoutError()
        self.assertEqual(self.send()['entries'][0]['delivery'], 'unknown')
        self.send()
        self.assertEqual(sum(x == 'turn/steer' for x, _ in self.rpc.calls), 1)
    def test_explicit_retry_preserves_prior_request_and_new_nonce(self):
        old = self.send()['entries'][0]['nonce']
        with patch.object(m, 'snapshot', return_value={}):
            r = m.request(self.rpc, self.c, self.report, retry_thread=TID)
        self.assertEqual(len(r['entries']), 1)
        self.assertEqual(r['superseded_requests'][0]['nonce'], old)
        self.assertNotEqual(r['entries'][0]['nonce'], old)
    def test_collects_new_staging_path_after_migration(self):
        r = self.send()
        e = r['entries'][0]
        old = e['path']
        e['path'] = str(self.root/'new-staging'/(TID+'.md'))
        self.write(e)
        e['path'] = old
        self.c['staging_dir'] = self.root/'new-staging'
        self.assertEqual(m.verify(self.c, r)['entries'][0]['verification'], 'written')
        self.assertEqual(e['path'], str(self.c['staging_dir']/(TID+'.md')))
    def test_race_never_starts_replacement_turn(self):
        self.rpc.turn['status'] = 'completed'
        self.send()
        self.assertFalse(any(x.startswith('turn/') for x, _ in self.rpc.calls))
    def test_acknowledgement_is_not_saved_handoff(self):
        e = self.send()['entries'][0]
        self.assertEqual(e['delivery'], 'accepted')
        self.assertEqual(m.verify_entry(e), 'missing')
    def test_valid_file_copied_private_and_unchanged_idle_skipped(self):
        r = self.send()
        e = r['entries'][0]
        self.write(e)
        v = m.verify(self.c, r)
        self.assertEqual(v['entries'][0]['verification'], 'written')
        archive = self.c['log_dir']/'threads'/TID/'MEMORY.md'
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.read_text(), Path(e['path']).read_text())
        self.rpc.thread['status'] = {'type': 'idle'}
        self.rpc.turn['status'] = 'completed'
        self.report = self.report.with_name('tomorrow.json')
        self.assertEqual(self.send()['entries'][0]['delivery'], 'unchanged')
        self.assertEqual(sum(x == 'turn/steer' for x, _ in self.rpc.calls), 1)
    def test_stale_marker_rejected(self):
        e = self.send()['entries'][0]
        self.write(e)
        e['nonce'] = 'replacement'
        self.assertEqual(m.verify_entry(e), 'stale')
    def test_completed_after_archive_requests_new_summary(self):
        r = self.send()
        self.write(r['entries'][0])
        m.verify(self.c, r)
        self.rpc.thread['status'] = {'type': 'idle'}
        self.rpc.turn.update(status='completed', completedAt=time.time()+10)
        self.report = self.report.with_name('tomorrow.json')
        self.assertEqual(self.send()['entries'][0]['delivery'], 'accepted')
        self.assertEqual(self.rpc.calls[-1][0], 'turn/start')
    def test_fifo_does_not_hang_verification(self):
        e = self.send()['entries'][0]
        os.mkfifo(e['path'], 0o600)
        self.assertEqual(m.verify_entry(e), 'unsafe-file')
    def test_stale_mtime_rejected(self):
        e = self.send()['entries'][0]
        p = self.write(e)
        os.utime(p, (0, 0))
        self.assertEqual(m.verify_entry(e), 'stale')
    def test_incomplete_and_oversize_rejected(self):
        e = self.send()['entries'][0]
        m.atomic(e['path'], m.marker(e)+'\n## Goal\nOnly a goal.')
        self.assertEqual(m.verify_entry(e), 'incomplete')
        m.atomic(e['path'], m.marker(e)+'\n'+'a'*12001)
        self.assertEqual(m.verify_entry(e), 'too-long')
    def test_world_readable_rejected(self):
        e = self.send()['entries'][0]
        self.write(e).chmod(0o644)
        self.assertEqual(m.verify_entry(e), 'not-private')
    def test_empty_section_cannot_consume_next_heading(self):
        e = self.send()['entries'][0]
        p = self.write(e)
        text = p.read_text().replace('## Goal\nRecorded evidence or None.\n', '## Goal\n')
        m.atomic(p, text)
        self.assertEqual(m.verify_entry(e), 'incomplete')
    def test_symlink_handoff_rejected(self):
        e = self.send()['entries'][0]
        p = Path(e['path'])
        p.symlink_to(self.root/'elsewhere')
        self.assertEqual(m.verify_entry(e), 'unsafe-path')
    def test_symlink_parent_rejected(self):
        (self.root/'linked').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(m.CheckpointError):
            m.atomic(self.root/'linked'/'nested'/'test', 'value')
    def test_other_session_keeps_own_file(self):
        r = self.send()
        p = self.write(r['entries'][0])
        original = p.read_text()
        self.rpc.thread['id'] = OTHER
        with patch.object(m, 'discover', return_value=[dict(thread=OTHER, cwd=str(self.root), status='active', turn_id='other', path=str(p.with_name(OTHER+'.md')))]):
            r = self.send()
        self.write(r['entries'][1])
        self.assertEqual(p.read_text(), original)
    def test_atomic_failure_preserves_old_file(self):
        p = self.root/'private'/'memory.md'
        m.atomic(p, 'old')
        with patch.object(m.os, 'replace', side_effect=OSError()):
            with self.assertRaises(OSError):
                m.atomic(p, 'new')
        self.assertEqual(p.read_text(), 'old')
    def test_lock_blocks_overlap(self):
        with m.locked(self.c):
            with self.assertRaises(BlockingIOError):
                with m.locked(self.c):
                    pass

class Installer(unittest.TestCase):
    def test_preserves_other_global_instructions_and_is_idempotent(self):
        spec = importlib.util.spec_from_file_location('checkpoint_install', Path(__file__).with_name('install.py'))
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        old = 'User instructions stay here.\n'
        one = installer.instructions(old, 'First block')
        self.assertTrue(one.startswith(old))
        self.assertEqual(installer.instructions(one, 'First block'), one)
        changed = installer.instructions(one, 'Replacement')
        self.assertTrue(changed.startswith(old))
        self.assertNotIn('First block', changed)
        self.assertIn('Replacement', changed)
        with self.assertRaises(RuntimeError):
            installer.instructions(installer.BEGIN, 'x')

class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = dict(enabled=True, log_dir=self.root/'central')

    def saved(self, thread=TID, age=0):
        cwd = self.root / 'project with spaces; $literal'
        cwd.mkdir(exist_ok=True)
        archive = self.c['log_dir'] / 'threads' / thread
        memory = archive / 'MEMORY.md'
        entry = dict(thread=thread, cwd=str(cwd), nonce='fixture', requested_at=0,
                     path=str(self.root/'staging'/(thread+'.md')))
        m.atomic(memory, m.marker(entry)+'\n'+''.join('## '+s+'\nNone.\n' for s in m.SECTIONS))
        saved = time.time()-age
        os.utime(memory, (saved, saved))
        m.atomic(archive/'receipt.json', entry)
        return entry, memory

    def run_recover(self, arguments=(), choices=(), tty=True, on_input=None):
        output, errors = io.StringIO(), io.StringIO()
        output.isatty = lambda: tty
        with redirect_stdout(output), redirect_stderr(errors), \
             patch.object(m.sys.stdin, 'isatty', return_value=tty), \
             patch('builtins.input', side_effect=on_input or list(choices)) as prompt, \
             patch.object(m, 'config_read', return_value=self.c), \
             patch.object(m, 'Rpc', side_effect=AssertionError('Recovery must not call RPC')), \
             patch.object(m.subprocess, 'call', return_value=7) as launch:
            result = m.main(['recover', *arguments])
        return result, output.getvalue(), errors.getvalue(), launch, prompt

    def test_menu_resumes_selected_uuid_with_memory_and_original_cwd(self):
        older, memory = self.saved(TID, age=60)
        self.saved(OTHER)
        result, output, _, launch, _ = self.run_recover(choices=['2'])
        self.assertEqual(result, 7)
        self.assertLess(output.index(OTHER), output.index(TID))
        command = launch.call_args.args[0]
        self.assertEqual(command[:5], ['codex', 'resume', '--cd', older['cwd'], TID])
        self.assertIn(str(memory), command[5])
        self.assertIn('first', command[5])
        self.assertIn('stay idle', command[5])
        self.assertEqual(launch.call_args.kwargs, {'cwd': older['cwd']})

    def test_bad_selection_reprompts_without_launching_other_session(self):
        self.saved()
        _, output, _, launch, prompt = self.run_recover(choices=['word', '0', '-1', '2', '1'])
        self.assertEqual(prompt.call_count, 5)
        self.assertEqual(output.count('Enter a number'), 4)
        launch.assert_called_once()

    def test_cancel_blank_quit_eof_and_interrupt_never_launch(self):
        self.saved()
        for choice, status in [('', 0), ('q', 0), ('QUIT', 0), (EOFError(), 0), (KeyboardInterrupt(), 130)]:
            with self.subTest(choice=repr(choice)):
                result, _, _, launch, _ = self.run_recover(choices=[choice])
                self.assertEqual(result, status)
                launch.assert_not_called()

    def test_list_and_redirected_output_never_prompt_or_launch(self):
        self.saved()
        for args, tty in [(('--list',), True), ((), False)]:
            _, output, _, launch, prompt = self.run_recover(args, tty=tty)
            self.assertIn(TID, output)
            prompt.assert_not_called()
            launch.assert_not_called()

    def test_explicit_thread_prints_shell_quoted_resume_command_only(self):
        entry, memory = self.saved()
        _, output, _, launch, prompt = self.run_recover(['--thread', TID])
        self.assertEqual(shlex.split(output), m.recovery_command(entry, memory))
        prompt.assert_not_called()
        launch.assert_not_called()

    def test_empty_archive_returns_without_input(self):
        result, output, _, launch, prompt = self.run_recover()
        self.assertEqual(result, 0)
        self.assertIn('No valid saved checkpoints', output)
        prompt.assert_not_called()
        launch.assert_not_called()

    def test_corrupt_receipt_and_invalid_memory_are_skipped(self):
        self.saved()
        _, bad = self.saved(OTHER)
        bad.chmod(0o644)
        m.atomic(self.c['log_dir']/'threads'/'broken'/'receipt.json', '{broken')
        _, output, errors, launch, _ = self.run_recover(['--list'])
        self.assertIn(TID, output)
        self.assertNotIn(OTHER, output)
        self.assertIn('Skipped 2', errors)
        launch.assert_not_called()

    def test_receipt_cannot_redirect_selected_thread(self):
        entry, memory = self.saved()
        entry['thread'] = OTHER
        m.atomic(memory.parent/'receipt.json', entry)
        with self.assertRaises(m.CheckpointError):
            m.recovery_entry(self.c, TID)

    def test_checkpoint_is_revalidated_after_selection(self):
        _, memory = self.saved()
        def choose(_):
            memory.unlink()
            return '1'
        with patch.object(m.subprocess, 'call') as launch:
            with self.assertRaises(m.CheckpointError):
                self.run_recover(on_input=choose)
            launch.assert_not_called()

    def test_missing_cwd_blocks_launch(self):
        entry, _ = self.saved()
        Path(entry['cwd']).rmdir()
        with self.assertRaisesRegex(m.CheckpointError, 'working directory is unavailable'):
            self.run_recover(choices=['1'])

if __name__ == '__main__':
    unittest.main()
