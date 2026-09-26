import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
import project_memory as m

T1='11111111-1111-4111-8111-111111111111'
T2='22222222-2222-4222-8222-222222222222'

class Memory(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.a=self.root/'project-a';self.a.mkdir()
        self.b=self.root/'project-b';self.b.mkdir()
        self.c=dict(enabled=True, log_dir=self.root/'central', staging_dir=self.root/'staging', socket=self.root/'socket')

    def add(self, cwd, thread, text, kind='decision', created=None):
        value=m.record(self.c,cwd,thread,kind,text,created)
        m.queue(self.c,value)
        return value

    def collect(self):
        return m.sync(self.c,use_rpc=False)

    def note(self, cwd, thread, goal):
        return (f'Thread ID: {thread}\nWorking directory: {cwd}\n'+
                ''.join('## '+s+'\n'+(goal if s=='Goal' else 'None')+'\n' for s in m.cp.SECTIONS))

    def test_same_project_combines_threads_and_other_project_is_never_returned(self):
        self.add(self.a,T1,'SQLite chosen because local search needs no network.')
        self.add(self.a,T2,'SQLite index must preserve provenance.')
        self.add(self.b,T1,'SQLite secret from different project.')
        self.assertEqual(self.collect()['added'],3)
        found=m.search(self.c,self.a,'SQLite')['results']
        self.assertEqual({r['thread'] for r in found},{T1,T2})
        self.assertEqual(len(found),2)
        self.assertNotIn('secret',str(found))
        foreign=m.search(self.c,self.b,'SQLite')['results'][0]['id']
        self.assertEqual(m.search(self.c,self.a,record_id=foreign)['results'],[])

    def test_duplicate_events_are_idempotent_and_checkpoint_versions_remain(self):
        old=self.note(self.a,T1,'Old design with SQLite')
        self.add(self.a,T1,old,'checkpoint',time.time()-10)
        self.add(self.a,T1,old,'checkpoint',time.time()-9)
        self.add(self.a,T1,self.note(self.a,T1,'New design with SQLite'),'checkpoint')
        self.assertEqual(self.collect()['added'],2)
        rows=m.search(self.c,self.a,'SQLite')['results']
        self.assertEqual(len(rows),2)
        self.assertEqual(sum(r['newer_checkpoint_exists'] for r in rows),1)
        self.assertEqual(self.collect()['added'],0)

    def test_read_only_search_does_not_create_database_or_staging(self):
        self.assertEqual(m.search(self.c,self.a,'something')['status'],'not-indexed')
        self.assertFalse(self.c['log_dir'].exists())
        self.assertFalse(self.c['staging_dir'].exists())

    def test_fts_search_quotes_untrusted_query_and_limits_results(self):
        for n in range(8):self.add(self.a,T1,f'SQLite useful decision {n}')
        self.collect()
        rows=m.search(self.c,self.a,'"SQLite" OR project:*; DROP TABLE memories',limit=2)['results']
        self.assertEqual(len(rows),2)
        self.assertEqual(len(m.search(self.c,self.a,limit=20)['results']),8)

    def test_checkpoint_cannot_cross_project_through_declared_directory(self):
        with self.assertRaisesRegex(m.MemoryError,'another project'):
            self.add(self.a,T1,self.note(self.b,T1,'foreign note'),'checkpoint')

    def test_unsafe_or_malformed_queue_files_do_not_poison_index(self):
        self.add(self.a,T1,'good local finding')
        inbox=self.c['staging_dir']/'project-memory-inbox'
        m.cp.atomic(inbox/'bad.json','{not json')
        m.cp.atomic(inbox/'public.json',json.dumps(m.record(self.c,self.b,T1,'finding','public file')))
        (inbox/'public.json').chmod(0o644)
        (inbox/'linked.json').symlink_to(inbox/'public.json')
        stats=self.collect()
        self.assertEqual(stats['added'],1)
        self.assertEqual(stats['skipped'],3)
        self.assertEqual(len(m.search(self.c,self.a)['results']),1)

    def test_queue_scope_cannot_be_changed_after_record_creation(self):
        value=m.record(self.c,self.a,T1,'finding','some observation')
        value['project']=m.project(self.c,self.b)[0]
        m.queue(self.c,value)
        self.assertEqual(self.collect()['skipped'],1)
        self.assertEqual(m.search(self.c,self.a)['results'],[])

    def test_checkpoint_validation_and_timestamps(self):
        for text in ('incomplete', self.note(self.a,T2,'wrong session')):
            with self.assertRaises(m.MemoryError):self.add(self.a,T1,text,'checkpoint')
        for stamp in (float('nan'),float('inf'),-1,time.time()+900):
            with self.assertRaises(m.MemoryError):self.add(self.a,T1,'bad date',created=stamp)

    def test_checkpoint_accepts_thread_header_variants(self):
        for header in (f'Thread: {T1}', f'Thread ID: {T1}',
                       f'thread id: `{T1}`', f'Thread:\t{T1}  '):
            with self.subTest(header=header):
                text=self.note(self.a,T1,'checkpoint').replace(f'Thread ID: {T1}',header)
                self.add(self.a,T1,text,'checkpoint')
        marker=f'<!-- checkpoint:fixture thread:{T1} -->\n'
        m.validate_checkpoint(marker+self.note(self.a,T1,'legacy').split('\n',1)[1],T1)

    def test_checkpoint_rejects_conflicting_bindings(self):
        for marker_thread, header_thread in ((T1,T2),(T2,T1)):
            text=f'<!-- checkpoint:fixture thread:{marker_thread} -->\n'+self.note(self.a,header_thread,'conflict')
            with self.subTest(marker=marker_thread,header=header_thread):
                with self.assertRaisesRegex(m.MemoryError,'identity does not match'):
                    m.validate_checkpoint(text,T1)
        with self.assertRaisesRegex(m.MemoryError,'identity does not match'):
            m.validate_checkpoint(f'Thread: {T2}\n'+self.note(self.a,T1,'conflict'),T1)

    def test_checkpoint_identity_errors_distinguish_missing_and_invalid(self):
        text=self.note(self.a,T1,'checkpoint')
        with self.assertRaisesRegex(m.MemoryError,'no thread identity'):
            m.validate_checkpoint(text.split('\n',1)[1],T1)
        for header in ('Thread:', 'Thread: invalid', f'Thread:\n{T1}'):
            with self.subTest(header=header):
                with self.assertRaisesRegex(m.MemoryError,'identity does not match'):
                    m.validate_checkpoint(text.replace(f'Thread ID: {T1}',header),T1)

    def test_capture_thread_label_queues_without_changing_checkpoint(self):
        text=self.note(self.a,T1,'viewer checkpoint').replace('Thread ID:', 'Thread:')
        path=self.c['staging_dir']/(T1+'.md');m.cp.atomic(path,text)
        with patch.object(m.cp,'config_read',return_value=self.c),patch.dict(os.environ,{'CODEX_THREAD_ID':T1}),redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['capture','--cwd',str(self.a)]),0)
        self.assertEqual(path.read_text(),text)
        self.assertEqual(self.collect()['added'],1)
        self.assertEqual(m.search(self.c,self.a,'viewer')['results'][0]['thread'],T1)

    def test_capture_conflicting_thread_never_queues(self):
        text=f'<!-- checkpoint:fixture thread:{T1} -->\n'+self.note(self.a,T2,'conflict')
        path=self.c['staging_dir']/(T1+'.md');m.cp.atomic(path,text)
        errors=io.StringIO()
        with patch.object(m.cp,'config_read',return_value=self.c),patch.dict(os.environ,{'CODEX_THREAD_ID':T1}),redirect_stderr(errors):
            self.assertEqual(m.main(['capture','--cwd',str(self.a)]),1)
        self.assertIn('identity does not match',errors.getvalue())
        self.assertNotIn(T1,errors.getvalue())
        self.assertFalse((self.c['staging_dir']/'project-memory-inbox').exists())
        self.assertEqual(path.read_text(),text)

    def test_private_files_and_exclusive_writer(self):
        self.add(self.a,T1,'private note')
        self.collect()
        self.assertEqual(m.database(self.c).stat().st_mode & 0o777,0o600)
        self.assertEqual(m.location(self.c).stat().st_mode & 0o777,0o700)
        with m.writer(self.c):
            with self.assertRaises(BlockingIOError):
                with m.writer(self.c):pass
        m.database(self.c).chmod(0o644)
        with self.assertRaises(m.MemoryError):m.search(self.c,self.a)

    def test_failed_transaction_retains_queue_for_retry(self):
        self.add(self.a,T1,'remember this')
        with patch.object(m,'insert',side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):self.collect()
        self.assertEqual(len(list((self.c['staging_dir']/'project-memory-inbox').glob('*.json'))),1)
        self.assertEqual(self.collect()['added'],1)

    def test_git_subdirectories_and_worktrees_share_but_nested_repo_is_separate(self):
        def git(*args):
            subprocess.run(['git',*map(str,args)],check=True,capture_output=True,
                           env={**os.environ,'GIT_AUTHOR_NAME':'Test','GIT_AUTHOR_EMAIL':'test@example.invalid',
                                'GIT_COMMITTER_NAME':'Test','GIT_COMMITTER_EMAIL':'test@example.invalid'})
        git('init','-b','main',self.a)
        git('-C',self.a,'commit','--allow-empty','-m','fixture')
        sub=self.a/'src';sub.mkdir()
        tree=self.root/'linked';git('-C',self.a,'worktree','add',tree)
        self.assertEqual(m.project(self.c,self.a)[0],m.project(self.c,sub)[0])
        self.assertEqual(m.project(self.c,self.a)[0],m.project(self.c,tree)[0])
        git('init',sub)
        self.assertNotEqual(m.project(self.c,self.a)[0],m.project(self.c,sub)[0])

    def test_non_git_roots_are_explicit_and_longest_match_wins(self):
        sub=self.a/'sub';sub.mkdir()
        self.assertNotEqual(m.project(self.c,self.a)[0],m.project(self.c,sub)[0])
        self.c['memory']={'project_roots':[str(self.a)]}
        self.assertEqual(m.project(self.c,self.a)[0],m.project(self.c,sub)[0])
        self.assertNotEqual(m.project(self.c,self.a)[0],m.project(self.c,self.b)[0])

    def test_directory_projects_work_without_git_installed(self):
        expected=m.project(self.c,self.a)
        with patch.object(m.subprocess,'run',side_effect=FileNotFoundError()):
            self.assertEqual(m.project(self.c,self.a),expected)

    def test_live_staging_is_collected_without_prompting(self):
        path=self.c['staging_dir']/(T1+'.md')
        m.cp.atomic(path,self.note(self.a,T1,'live progress'))
        rpc=unittest.mock.Mock()
        with patch.object(m.cp,'Rpc',return_value=rpc),patch.object(m.cp,'discover',return_value=[dict(thread=T1,cwd=str(self.a))]):
            stats=m.sync(self.c)
        self.assertTrue(stats['rpc_available'])
        self.assertEqual(stats['added'],1)
        rpc.call.assert_not_called()
        rpc.close.assert_called_once()

    def test_rpc_outage_still_collects_queued_notes(self):
        self.add(self.a,T1,'offline note')
        with patch.object(m.cp,'Rpc',side_effect=OSError()):stats=m.sync(self.c)
        self.assertEqual(stats['added'],1)
        self.assertFalse(stats['rpc_available'])

    def test_recovery_context_is_bounded_and_excludes_self_and_other_projects(self):
        self.add(self.a,T1,'SQLite own note')
        self.add(self.a,T2,'SQLite peer evidence')
        self.add(self.b,T2,'SQLite foreign secret')
        self.collect()
        path=self.root/'MEMORY.md';m.cp.atomic(path,self.note(self.a,T1,'SQLite recovery'))
        context=m.recovery_context(self.c,dict(thread=T1,cwd=str(self.a)),path)
        self.assertIn('peer evidence',context)
        self.assertNotIn('own note',context)
        self.assertNotIn('foreign secret',context)
        self.assertIn('does not assign work',context)
        m.database(self.c).write_bytes(b'broken database')
        self.assertEqual(m.recovery_context(self.c,dict(thread=T1,cwd=str(self.a)),path),'')

    def test_capture_uses_real_thread_environment_and_retains_snapshot(self):
        path=self.c['staging_dir']/(T1+'.md');text=self.note(self.a,T1,'checkpoint evidence')
        m.cp.atomic(path,text)
        with patch.object(m.cp,'config_read',return_value=self.c),patch.dict(os.environ,{'CODEX_THREAD_ID':T1}),redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['capture','--cwd',str(self.a)]),0)
        m.cp.atomic(path,self.note(self.a,T1,'newer overwritten state'))
        self.collect()
        result=m.search(self.c,self.a,'evidence')['results']
        self.assertEqual(len(result),1)
        full=m.search(self.c,self.a,record_id=result[0]['id'])['results'][0]['text']
        self.assertEqual(full,text)

if __name__=='__main__':unittest.main()
