"""Lost Claude compaction hooks, using only temporary monitor and session files."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('monitor_compaction_tests',
                                            Path(__file__).resolve().parents[1] / 'scripts/monitor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CompactionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.now = 10_000
        for name, value in (('DATA', root / 'state'), ('CLAUDE', root / 'claude')):
            p = patch.object(m, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (patch.object(m.time, 'time', return_value=self.now),
                  patch.object(m, 'alive', return_value=True),
                  patch.object(m, 'codex_open_rollouts', return_value={})):
            p.start()
            self.addCleanup(p.stop)
        m.save_config({'avatars': {'Test': str(root / 'avatar')}})
        self.transcript = root / 'chat.jsonl'
        self.transcript.write_text('{}\n')
        os.utime(self.transcript, (9_000, 9_000))
        self.metadata = m.CLAUDE / 'sessions' / '123.json'
        self.metadata.parent.mkdir(parents=True)
        self.write_metadata('idle', 8_000)
        self.row = dict(m.blank('chat'), provider='claude', pid=123, proc_start='1', cwd='/demo',
                        state='compacting', source='hook', updated=9_600, created=8_000,
                        started=8_000, turn_started=9_500, transcript=str(self.transcript), avatar='Test')
        self.save_row()

    def write_metadata(self, state, stamp):
        self.metadata.write_text(json.dumps({'sessionId': 'chat', 'pid': 123, 'procStart': '1',
                                            'kind': 'interactive', 'status': state,
                                            'statusUpdatedAt': stamp * 1000}))

    def save_row(self):
        with m.connect() as db:
            m.save(db, self.row)

    def refresh(self):
        return m.refresh_sessions()[0]

    def test_old_idle_report_recovers_and_persists_without_inflating_active_time(self):
        row = self.refresh()
        self.assertEqual((row['state'], row['source']), ('idle', 'metadata'))
        self.assertEqual(row['updated'], 9_600)
        self.assertIsNone(row['turn_started'])
        self.assertEqual((row['last_turn'], row['busy']), (100, 100))
        self.assertEqual(m.status_text(row), 'Idle · detected')
        with sqlite3.connect(m.DATA / 'sessions.sqlite3') as db:
            self.assertEqual(db.execute('SELECT state, turn_started FROM sessions').fetchone(), ('idle', None))
        self.assertEqual(self.refresh(), row)  # old metadata must not rewind the recovered timestamp
        self.write_metadata('busy', 8_500)
        self.assertEqual(self.refresh()['state'], 'idle')  # or resurrect stale activity
        self.write_metadata('busy', 9_900)
        self.assertEqual(self.refresh()['state'], 'thinking')

    def test_recent_start_or_transcript_activity_keeps_compacting(self):
        for event_time, transcript_time in ((9_800, 9_000), (9_600, 9_800)):
            with self.subTest(event_time=event_time, transcript_time=transcript_time):
                self.row['updated'] = event_time
                self.save_row()
                os.utime(self.transcript, (transcript_time, transcript_time))
                self.assertEqual(self.refresh()['state'], 'compacting')

    def test_requires_explicit_idle_and_existing_transcript(self):
        for status in ('busy', 'waiting', None):
            with self.subTest(status=status):
                self.write_metadata(status, 8_000)
                self.assertEqual(self.refresh()['state'], 'compacting')
        self.write_metadata('idle', 8_000)
        self.transcript.unlink()
        self.assertEqual(self.refresh()['state'], 'compacting')

    def test_orphaned_chat_without_metadata_or_transcript_recovers_after_hook_silence(self):
        self.metadata.unlink()
        self.assertEqual(self.refresh()['state'], 'compacting')  # its transcript is still there
        self.transcript.unlink()
        row = self.refresh()
        self.assertEqual((row['state'], row['source'], row['updated']), ('idle', 'metadata', 9_600))
        self.assertEqual(self.refresh(), row)

    def test_orphaned_chat_waits_for_grace_period_and_respects_disable(self):
        self.metadata.unlink()
        self.transcript.unlink()
        self.row['updated'] = 9_800
        self.save_row()
        self.assertEqual(self.refresh()['state'], 'compacting')
        self.row['updated'] = 9_600
        self.save_row()
        m.save_config({'avatars': {'Test': '/demo'}, 'compact_stale_after': 0})
        self.assertEqual(self.refresh()['state'], 'compacting')

    def test_configurable_grace_period_and_disable(self):
        for timeout in (0, 600):
            with self.subTest(timeout=timeout):
                m.save_config({'avatars': {'Test': '/demo'}, 'compact_stale_after': timeout})
                self.assertEqual(self.refresh()['state'], 'compacting')
        m.save_config({'avatars': {'Test': '/demo'}, 'compact_stale_after': 400})
        self.assertEqual(self.refresh()['state'], 'idle')

    def test_alerts_other_activity_and_codex_are_untouched(self):
        for state in ('waiting', 'failed', 'thinking', 'reading', 'working', 'delegating'):
            with self.subTest(state=state):
                self.row.update(state=state, source='hook')
                self.save_row()
                self.assertEqual(self.refresh()['state'], state)
        self.row.update(state='compacting', provider='codex')
        self.save_row()
        self.metadata.unlink()  # Codex does not have Claude session metadata
        with patch.object(m, 'codex_turn_state', return_value=('thinking', 9_500)):
            self.assertEqual(self.refresh()['state'], 'compacting')

    def test_completion_events_and_new_work_clear_compacting(self):
        for event, payload in (('SessionStart', {'source': 'compact'}), ('PostCompact', {}),
                               ('UserPromptSubmit', {})):
            with self.subTest(event=event):
                row = dict(self.row)
                m.apply(row, event, payload, self.now)
                self.assertEqual(row['state'], 'thinking')
        row = dict(self.row, state='waiting', waiters='[""]')
        m.apply(row, 'SessionStart', {'source': 'compact'}, self.now)
        self.assertEqual(row['state'], 'waiting')
        # Hooks resume normal tracking after the persisted fallback.
        self.refresh()
        m.record({'session_id': 'chat', 'hook_event_name': 'UserPromptSubmit',
                  'transcript_path': str(self.transcript)}, owner=(123, '1'))
        row = self.refresh()
        self.assertEqual((row['state'], row['source'], row['turn_started']), ('thinking', 'hook', self.now))


if __name__ == '__main__':
    unittest.main()
