import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

SPEC = importlib.util.spec_from_file_location('monitor', Path(__file__).resolve().parents[1] / 'scripts/monitor.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        m.DATA = Path(self.tmp.name) / 'state'
        m.CLAUDE = Path(self.tmp.name) / 'claude'
        m.CLAUDE_JSON = Path(self.tmp.name) / '.claude.json'
        m.CODEX = Path(self.tmp.name) / 'codex'
        m._usage.clear()
        m._codex_usage.clear()
        m._codex_titles[:] = [None, {}]
        m._codex_names.clear()
        m._codex_scan[:] = [0.0, {}]
        m._codex_meta.clear()
        m._codex_turns.clear()
        self.real_comm = m.CODEX_COMM
        self.real_fetch = m.fetch_usage
        self.owner = (os.getpid(), m.process_info(os.getpid())['start'])

    def tearDown(self):
        m.fetch_usage = self.real_fetch
        m.CODEX_COMM = self.real_comm
        self.tmp.cleanup()

    def event(self, event, sid='chat-a', **extra):
        m.record(dict(session_id=sid, hook_event_name=event, cwd='/work/demo', **extra), self.owner)

    def state(self):
        return m.refresh_sessions()[0]['state']

    def test_full_lifecycle_and_no_transcript_storage(self):
        self.event('SessionStart')
        self.assertEqual(self.state(), 'idle')
        self.event('UserPromptSubmit', prompt='DO NOT STORE THIS SECRET')
        self.assertEqual(self.state(), 'thinking')
        self.event('PermissionRequest', tool_input={'secret': 'NEVER RETAIN'})
        self.assertEqual(self.state(), 'waiting')
        self.event('PostToolUse')
        self.assertEqual(self.state(), 'thinking')
        self.event('PostToolUseFailure', error='NEVER RETAIN EITHER')
        row = m.refresh_sessions()[0]
        self.assertEqual((row['state'], row['tool_errors'], row['flash']), ('thinking', 1, 'failed'))
        self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'SECRET COMMAND'})
        self.assertEqual(self.state(), 'working')
        self.event('Stop')
        self.assertEqual(self.state(), 'ready')
        self.event('UserPromptSubmit')
        self.event('StopFailure')
        self.assertEqual(self.state(), 'failed')
        data = (m.DATA / 'sessions.sqlite3').read_bytes()
        self.assertNotIn(b'DO NOT STORE', data)
        self.assertNotIn(b'NEVER RETAIN', data)
        self.assertNotIn(b'SECRET COMMAND', data)
        self.event('SessionEnd')
        self.assertEqual(m.refresh_sessions(), [])

    def test_parallel_chats_are_independent_and_assignments_stable(self):
        c = m.config(); c['avatars']['Other'] = c['avatars']['Tifa']; m.save_config(c)
        self.event('SessionStart', 'first')
        self.event('SessionStart', 'second')
        self.assertEqual([x['avatar'] for x in m.refresh_sessions()], ['Tifa', 'Other'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.event('UserPromptSubmit', f'chat-{n}'), range(20)))
        self.assertEqual(len(m.refresh_sessions()), 22)
        self.event('PermissionRequest', 'first')
        self.event('StopFailure', 'second')
        rows = {r['id']: r for r in m.refresh_sessions()}
        self.assertEqual(rows['first']['state'], 'waiting')
        self.assertEqual(rows['second']['state'], 'failed')
        self.assertEqual(rows['second']['avatar'], 'Other')

    def test_dead_or_reused_pid_removed(self):
        self.event('SessionStart')
        with m.connect() as db:
            db.execute('UPDATE sessions SET proc_start=?', ('wrong-start-time',))
        self.assertEqual(m.refresh_sessions(), [])

    def test_bootstrap_ignores_dead_sessions_and_preserves_hook_alerts(self):
        (m.CLAUDE / 'sessions').mkdir(parents=True)
        meta = dict(sessionId='existing', pid=self.owner[0], procStart=self.owner[1],
                    kind='interactive', cwd='/project', status='busy', statusUpdatedAt=time.time()*1000)
        p = m.CLAUDE / 'sessions/one.json'; p.write_text(json.dumps(meta))
        self.assertEqual(self.state(), 'thinking')
        self.event('PermissionRequest', 'existing')
        meta.update(status='idle', statusUpdatedAt=(time.time()+1)*1000)
        p.write_text(json.dumps(meta))
        self.assertEqual(self.state(), 'waiting')
        self.event('SessionEnd', 'existing')
        self.assertEqual(m.refresh_sessions(), [])
        self.event('SessionStart', 'existing')
        self.assertEqual(self.state(), 'idle')
        meta['procStart'] = 'not-the-same-process'
        p.write_text(json.dumps(meta))
        self.event('SessionEnd', 'existing')
        self.assertEqual(m.refresh_sessions(), [])

    def test_installer_roundtrip_preserves_other_settings(self):
        other = {'model': 'custom', 'permissions': {'allow': ['Read']},
                 'hooks': {'Stop': [{'matcher': '', 'hooks': [{'type':'command','command':'notify-other'}]}]}}
        installed = m.merge_hooks(other)
        self.assertEqual(installed, m.merge_hooks(installed))
        self.assertEqual(m.merge_hooks(installed, remove=True), other)
        m.CLAUDE.mkdir()
        p = m.CLAUDE / 'settings.json'; p.write_text(json.dumps(other))
        m.install_hooks()
        self.assertEqual(json.loads(p.read_text()), installed)
        self.assertEqual(len(list(m.CLAUDE.glob('*backup*'))), 1)
        m.install_hooks(remove=True)
        self.assertEqual(json.loads(p.read_text()), other)

    def test_unknown_events_and_notification_do_not_change_state(self):
        self.event('MadeUpEvent')
        self.assertEqual(m.refresh_sessions(), [])
        self.event('UserPromptSubmit')
        self.event('Notification', notification_type='auth_success')
        self.assertEqual(self.state(), 'thinking')
        self.event('PreToolUse', tool_name='AskUserQuestion')
        self.assertEqual(self.state(), 'waiting')


    def test_tool_categories_and_lifecycle_states(self):
        self.event('UserPromptSubmit')
        for tool, state in (('Read', 'reading'), ('Grep', 'reading'), ('WebSearch', 'reading'),
                            ('Edit', 'working'), ('Bash', 'working'), ('mcp__x__y', 'working'),
                            ('Agent', 'delegating'), ('AskUserQuestion', 'waiting'), ('ExitPlanMode', 'waiting')):
            self.event('PreToolUse', tool_name=tool)
            self.assertEqual(self.state(), state, tool)
            self.event('PostToolUse', tool_name=tool)
            self.assertEqual(self.state(), 'thinking')
        self.event('PreCompact')
        self.assertEqual(self.state(), 'compacting')
        self.event('SessionStart', source='compact')
        self.assertEqual(self.state(), 'compacting')
        self.event('PostCompact')
        self.assertEqual(self.state(), 'thinking')
        self.event('PreToolUse', tool_name='Bash')
        self.event('PostToolUseFailure', is_interrupt=True)
        row = m.refresh_sessions()[0]
        self.assertEqual((row['state'], row['tool_errors'], row['turn_started']), ('idle', 0, None))

    def test_subagents_keep_parent_delegating(self):
        self.event('UserPromptSubmit')
        self.event('PreToolUse', tool_name='Agent')
        self.event('SubagentStart', agent_id='a1')
        self.event('PreToolUse', tool_name='Read', agent_id='a1')
        self.assertEqual(self.state(), 'delegating')
        self.event('PermissionRequest', agent_id='a1')
        self.assertEqual(self.state(), 'waiting')
        self.event('PostToolUse', tool_name='Read', agent_id='a1')
        row = m.refresh_sessions()[0]
        self.assertEqual((row['state'], row['tools']), ('delegating', 2))
        self.assertIn('×1', m.status_text(row))
        self.event('SubagentStop', agent_id='a1')
        self.event('PostToolUse', tool_name='Agent')
        row = m.refresh_sessions()[0]
        self.assertEqual((row['state'], json.loads(row['subagents'])), ('thinking', {}))
        self.event('Stop')
        self.event('PreToolUse', tool_name='Bash', agent_id='background')
        self.assertEqual(self.state(), 'ready')

    def test_turn_timing_counters_and_animation(self):
        self.event('UserPromptSubmit')
        with m.connect() as db:
            db.execute('UPDATE sessions SET turn_started=turn_started-90, started=started-4000')
        self.event('PreToolUse', tool_name='Edit')
        row = m.refresh_sessions()[0]
        self.assertEqual(row['tool'], 'Edit')
        self.assertIn('Working · Edit', m.status_text(row))
        text = '\n'.join(m.details(row, None, time.time()))
        self.assertIn('session 1h 06m', text)
        self.assertIn('this turn 1m 30s', text)
        self.assertIn('1 prompts · 1 tools', text)
        self.event('Stop')
        row = m.refresh_sessions()[0]
        self.assertGreaterEqual(row['last_turn'], 90)
        self.assertGreaterEqual(row['busy'], 90)
        self.assertIsNone(row['turn_started'])
        now = time.time()
        self.assertEqual(m.animation(row, now), 'celebrate')
        self.assertEqual(m.animation(row, now + 5), 'ready')
        self.assertEqual(m.animation(row, now + m.READY_WAVE + 5), 'idle')

    def test_interrupt_detected_from_metadata_ends_busy_turn(self):
        (m.CLAUDE / 'sessions').mkdir(parents=True)
        self.event('UserPromptSubmit', 'live')
        self.event('PreToolUse', 'live', tool_name='Read')
        meta = dict(sessionId='live', pid=self.owner[0], procStart=self.owner[1], kind='interactive',
                    cwd='/work/demo', status='idle', statusUpdatedAt=(time.time()+1)*1000,
                    startedAt=(time.time()-600)*1000, name='demo-7c', nameSource='derived')
        (m.CLAUDE / 'sessions/live.json').write_text(json.dumps(meta))
        row = m.refresh_sessions()[0]
        self.assertEqual((row['state'], row['name'], row['turn_started']), ('idle', None, None))
        self.assertLess(abs(row['started'] - (time.time() - 600)), 2)
        meta.update(name='Auth work', nameSource='user')  # /rename
        (m.CLAUDE / 'sessions/live.json').write_text(json.dumps(meta))
        m.record(dict(session_id='live', hook_event_name='PreToolUse', cwd='/work/demo/sub'), self.owner)
        row = m.refresh_sessions()[0]
        self.assertEqual((row['name'], row['cwd']), ('Auth work', '/work/demo'))

    def test_usage_reader_dedupes_streams_and_reads_incrementally(self):
        path = Path(self.tmp.name) / 'proj' / 'chat-a.jsonl'
        path.parent.mkdir()
        def assistant(mid, i, cr, o, side=False):
            return json.dumps({'type': 'assistant', 'isSidechain': side, 'message': {
                'id': mid, 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'PRIVATE'}],
                'usage': {'input_tokens': i, 'cache_creation_input_tokens': 0,
                          'cache_read_input_tokens': cr, 'output_tokens': o}}}) + '\n'
        path.write_text(json.dumps({'type': 'user', 'message': {'content': 'usage "assistant"'}}) + '\n'
                        + assistant('m1', 10, 1000, 5) + assistant('m1', 10, 1000, 50))
        self.event('UserPromptSubmit', transcript_path=str(path))
        u = m.session_usage(m.refresh_sessions()[0])
        self.assertEqual((u['context'], u['output'], u['input'], u['requests']), (1010, 50, 1010, 1))
        self.assertEqual(u['window'], 1_000_000)
        with open(path, 'a') as f:
            f.write(assistant('m2', 20, 3000, 7, side=True))
            f.write(json.dumps({'type': 'system', 'subtype': 'compact_boundary'}) + '\n')
            f.write(assistant('m3', 30, 2000, 9)[:40])  # partially written line
        u = m.session_usage(m.refresh_sessions()[0])
        self.assertEqual((u['context'], u['output'], u['requests'], u['compactions']), (1010, 57, 2, 1))
        with open(path, 'a') as f:
            f.write(assistant('m3', 30, 2000, 9)[40:])
        u = m.session_usage(m.refresh_sessions()[0])
        self.assertEqual((u['context'], u['output'], u['requests']), (2030, 66, 3))
        sub = path.with_suffix('') / 'subagents' / 'agent-1.jsonl'
        sub.parent.mkdir(parents=True)
        sub.write_text(assistant('s1', 1, 0, 100))
        u = m.session_usage(m.refresh_sessions()[0])
        self.assertEqual((u['context'], u['output']), (2030, 166))
        text = '\n'.join(m.details(m.refresh_sessions()[0], u, time.time()))
        self.assertIn('context 2k/1M · 0%', text)
        self.assertIn('opus-5-5 · 4 calls · 1 compact', text)

    def test_chat_title_prefers_custom_title_over_ai_title(self):
        path = Path(self.tmp.name) / 'proj' / 'chat-t.jsonl'
        path.parent.mkdir()
        def title(kind, key, text):
            return json.dumps({'type': kind, key: text, 'sessionId': 'chat-t'}, separators=(',', ':')) + '\n'
        path.write_text(json.dumps({'type': 'user', 'message': {'content': '"type":"ai-title" PRIVATE'}}) + '\n')
        self.event('UserPromptSubmit', transcript_path=str(path))
        self.assertIsNone(m.session_usage(m.refresh_sessions()[0])['title'])
        with open(path, 'a') as f:
            f.write(title('ai-title', 'aiTitle', 'Fix  the\nlogin bug'))
        self.assertEqual(m.session_usage(m.refresh_sessions()[0])['title'], 'Fix the login bug')
        with open(path, 'a') as f:
            f.write(title('custom-title', 'customTitle', 'Auth work'))
            f.write(title('ai-title', 'aiTitle', 'Later AI title'))
        self.assertEqual(m.session_usage(m.refresh_sessions()[0])['title'], 'Auth work')

    def test_old_database_is_migrated(self):
        import sqlite3
        m.DATA.mkdir(parents=True)
        db = sqlite3.connect(m.DATA / 'sessions.sqlite3')
        db.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, pid INTEGER, proc_start TEXT,
                      state TEXT, updated REAL, source TEXT, avatar TEXT, created REAL)""")
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)',
                   ('chat-a', '/work/demo', *self.owner, 'working', time.time(), 'hook', 'Tifa', time.time()))
        db.commit(); db.close()
        self.event('PostToolUse')
        row = m.refresh_sessions()[0]
        self.assertEqual((row['state'], row['tools'], row['subagents']), ('thinking', 0, '{}'))

    def test_formatting_and_asset_fallbacks(self):
        self.assertEqual([m.duration(s) for s in (5, 75, 900, 3725)], ['5s', '1m 15s', '15m', '1h 02m'])
        self.assertEqual([m.tokens(n) for n in (950, 12345, 1_000_000, 2_340_000)], ['950', '12.3k', '1M', '2.3M'])
        tifa = m.ROOT / 'assets/tifa'
        if (tifa / 'idle.gif').exists():
            for anim in m.ANIMATIONS:
                self.assertTrue(m.gif_for(tifa, anim).exists(), anim)
            self.assertEqual(m.gif_for(tifa, 'background').name, 'piano.gif')
            self.assertEqual(m.gif_for(tifa, 'thinking').name, 'running.gif')
            selected = {m.gif_for(tifa, anim).name for anim in m.ANIMATIONS}
            self.assertFalse(selected & {'running-right.gif', 'look-loop.gif', 'idle-jump-idle.gif', 'jumping.gif', 'waving.gif'})
        bare = Path(self.tmp.name) / 'bare'; bare.mkdir()
        for name in m.REQUIRED:
            (bare / (name + '.gif')).write_bytes(b'')
        self.assertEqual(m.gif_for(bare, 'reading').name, 'running.gif')
        self.assertEqual(m.gif_for(bare, 'celebrate').name, 'waving.gif')
        (bare / 'animations.json').write_text(json.dumps({'idle': ['piano', 'idle']}))
        self.assertEqual(m.gif_for(bare, 'idle').name, 'idle.gif')
        (bare / 'piano.gif').write_bytes(b'')
        self.assertEqual(m.gif_for(bare, 'idle').name, 'piano.gif')
        (bare / 'animations.json').write_text(json.dumps({'idle': ['../outside']}))
        with self.assertRaises(ValueError):
            m.gif_for(bare, 'idle')

    def test_themed_scenes_match_task_states(self):
        tifa = m.ROOT / 'assets/tifa'
        if not (tifa / 'idle.gif').exists():
            self.skipTest('Personal sprite assets are excluded from Git')
        expected = {'idle': 'idle', 'background': 'piano', 'working': 'bomb-combo',
                    'thinking': 'running', 'reading': 'materia-study', 'compacting': 'running',
                    'waiting': 'waiting', 'delegating': 'waiting', 'failed': 'failed',
                    'ready': 'idle', 'celebrate': 'idle'}
        for state, scene in expected.items():
            self.assertEqual(m.gif_for(tifa, state).stem, scene, state)
        from PIL import Image, ImageChops
        for scene in ('piano', 'bomb-combo', 'materia-study'):
            with Image.open(tifa / (scene + '.gif')) as im:
                self.assertEqual(im.n_frames, 6)
                self.assertEqual(im.info['loop'], 0)
                frames = []
                for i in range(6):
                    im.seek(i)
                    frame = im.convert('RGBA')
                    self.assertEqual(frame.getpixel((0,0))[3], 0)
                    frames.append(frame.convert('RGB'))
                self.assertTrue(any(ImageChops.difference(frames[0], f).getbbox() for f in frames[1:]))

    def test_character_profiles_and_execution_only_rotation(self):
        import random
        from PIL import Image
        if not (m.ROOT / 'assets/tifa/idle.gif').exists():
            self.skipTest('Personal sprite assets are excluded from Git')
        for character in ('tifa', 'rinoa', 'yuna'):
            directory = m.ROOT / 'assets' / character
            for state in m.ANIMATIONS:
                for path in m.scene_paths(directory, state):
                    with Image.open(path) as im:
                        self.assertEqual(m.fit_sprite(im).size, (168,126))
            scenes = [p.stem for p in m.scene_paths(directory, 'working')]
            self.assertGreaterEqual(len(scenes), 2)
            card = {}; rng = random.Random(4)
            first = m.choose_scene(card, character, 'working', scenes, 100, rng)
            self.assertEqual(m.choose_scene(card, character, 'working', scenes, 101, rng), first)
            self.assertTrue(108 <= card['encounter_until'] <= 114)
            self.assertEqual(m.choose_scene(card, character, 'waiting', ['await-command'], 102, rng),
                             (character,'waiting','await-command'))
            next_key = m.choose_scene(card, character, 'working', scenes, 115, rng)
            self.assertNotEqual(next_key, first)
            self.assertEqual(len(m.scene_paths(directory, 'failed')), 1)
        shape = Image.new('RGBA',(100,50),(255,0,0,255))
        fitted = m.fit_sprite(shape)
        # thumbnail does not upscale this smaller source; proportions remain 2:1.
        x1,y1,x2,y2 = fitted.getbbox()
        self.assertEqual((x2-x1)/(y2-y1), 2)
        self.assertEqual(fitted.width/fitted.height, 4/3)

    def test_neutral_idle_and_reading_are_separate(self):
        if not (m.ROOT / 'assets/tifa/idle.gif').exists():
            self.skipTest('Personal sprite assets are excluded from Git')
        for character, scene in [('tifa','idle'), ('rinoa','await-command'), ('yuna','await-command')]:
            directory = m.ROOT / 'assets' / character
            # Picker thumbnails resolve this same idle asset.
            self.assertEqual(m.gif_for(directory, 'idle').stem, scene)
            self.assertNotEqual(m.gif_for(directory, 'idle'), m.gif_for(directory, 'background'))
        self.assertEqual(m.gif_for(m.ROOT / 'assets/tifa','reading').stem, 'materia-study')
        self.assertEqual(m.gif_for(m.ROOT / 'assets/tifa','thinking').stem, 'running')

    def test_state_icons_are_distinct_and_nonblank(self):
        pixels = {}
        for state in m.LABELS:
            icon = m.draw_icon(state, size=20)
            self.assertEqual(icon.size, (20, 20))
            self.assertEqual(icon.mode, 'RGBA')
            opaque = sum(1 for p in icon.getdata() if p[3] > 0)
            self.assertGreater(opaque, 15, state)  # not a blank/near-empty canvas
            pixels[state] = icon.tobytes()
        self.assertEqual(len(set(pixels.values())), len(pixels))  # every state looks different
        self.assertEqual(m.draw_icon('no-such-state').size, (18, 18))  # falls back, doesn't raise

    def notice(self, path, use_id, status='completed'):
        body = (f'<task-notification><task-id>b1</task-id><tool-use-id>{use_id}</tool-use-id>'
                f'<status>{status}</status><summary>PRIVATE SUMMARY</summary></task-notification>')
        with open(path, 'a') as f:
            f.write(json.dumps({'type': 'queue-operation', 'operation': 'enqueue', 'content': body}) + '\n')

    def live(self):
        row = m.refresh_sessions()[0]
        return m.reconcile(row, m.session_usage(row))

    def test_background_jobs_until_task_notification(self):
        path = Path(self.tmp.name) / 'proj' / 'chat-a.jsonl'
        path.parent.mkdir(); path.write_text('')
        self.event('UserPromptSubmit', transcript_path=str(path))
        self.event('PreToolUse', tool_name='Bash', tool_use_id='toolu_bg1',
                   tool_input={'command': 'SECRET', 'run_in_background': True})
        self.event('PreToolUse', tool_name='Monitor', tool_use_id='toolu_mon')
        self.event('PreToolUse', tool_name='Bash', tool_use_id='toolu_fg', tool_input={'command': 'ls'})
        self.event('PreToolUse', tool_name='Bash', tool_use_id='toolu_denied', tool_input={'run_in_background': True})
        self.event('PermissionDenied', tool_name='Bash', tool_use_id='toolu_denied')
        self.assertEqual(set(json.loads(self.live()['jobs'])), {'toolu_bg1', 'toolu_mon'})
        self.event('Stop')
        row = self.live()
        self.assertEqual((row['state'], m.status_text(row)), ('background', 'Background ×2'))
        self.assertEqual(m.animation(row, time.time()), 'background')  # no celebration yet
        self.assertIn('background 2 jobs', m.details(row, None, time.time()))
        self.notice(path, 'toolu_bg1')
        self.assertEqual(self.live()['pending_jobs'], 1)
        # Claude is woken by the notice: that work counts as a turn without a user prompt.
        self.event('PreToolUse', tool_name='Read')
        self.assertIsNotNone(m.refresh_sessions()[0]['turn_started'])
        self.event('Stop')
        self.assertEqual(self.live()['state'], 'background')
        self.notice(path, 'toolu_mon', 'killed')
        row = self.live()
        self.assertEqual((row['state'], row['pending_jobs']), ('ready', 0))
        self.assertGreater(row['last_turn'], -1)
        self.assertNotIn(b'PRIVATE SUMMARY', (m.DATA / 'sessions.sqlite3').read_bytes())
        self.assertNotIn(b'SECRET', (m.DATA / 'sessions.sqlite3').read_bytes())

    def test_background_agents_and_alerts(self):
        self.event('UserPromptSubmit')
        self.event('PreToolUse', tool_name='Agent', tool_use_id='toolu_ag', tool_input={'run_in_background': True})
        self.event('SubagentStart', agent_id='bg-agent')
        self.event('PostToolUse', tool_name='Agent', tool_use_id='toolu_ag')
        self.event('Stop')
        row = self.live()
        self.assertEqual((row['state'], row['pending_jobs'], row['pending_agents']), ('background', 0, 1))
        self.event('PermissionRequest', agent_id='bg-agent')
        self.assertEqual(self.live()['state'], 'waiting')
        self.event('PostToolUse', agent_id='bg-agent')
        self.event('SubagentStop', agent_id='bg-agent')
        self.event('Stop')
        self.assertEqual(self.live()['state'], 'ready')
        self.event('UserPromptSubmit')
        self.event('PreToolUse', tool_name='Bash', tool_use_id='toolu_x', tool_input={'run_in_background': True})
        self.event('StopFailure')
        self.assertEqual(self.live()['state'], 'failed')


    def sign_in(self, org='Eastworld', kind='claude_team', email='me@example.com', uuid='u1', cache=None):
        d = {'oauthAccount': {'emailAddress': email, 'organizationName': org, 'organizationType': kind,
                              'organizationRole': 'user', 'accountUuid': uuid,
                              'userRateLimitTier': 'default_claude_max_5x'}}
        if cache:
            d['cachedUsageUtilization'] = cache
        m.CLAUDE_JSON.write_text(json.dumps(d))
        m.CLAUDE.mkdir(exist_ok=True)
        (m.CLAUDE / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'sk-SECRET-TOKEN', 'expiresAt': (time.time() + 3600) * 1000}}))

    def api(self, five, week, five_reset=3600, week_reset=86400 * 3):
        iso = lambda dt: time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(time.time() + dt))
        return {'five_hour': {'utilization': five, 'resets_at': iso(five_reset)},
                'seven_day': {'utilization': week, 'resets_at': iso(week_reset)},
                'limits': [{'kind': 'weekly_scoped', 'percent': 3, 'resets_at': iso(week_reset)}]}

    def test_account_and_poll_never_store_the_token(self):
        self.sign_in()
        self.assertEqual(m.account(), {'email': 'me@example.com', 'org': 'Eastworld', 'plan': 'Team',
                                       'tier': 'Max 5x', 'role': 'user'})
        seen = []
        m.fetch_usage = lambda token: seen.append(token) or self.api(2.0, 20.0)
        state = m.poll_limits()
        self.assertEqual(seen, ['sk-SECRET-TOKEN'])
        self.assertEqual((state['source'], state['error'], state['extra'][0]['kind']), ('api', None, 'weekly_scoped'))
        view = m.limit_view(state, time.time())
        self.assertEqual((view['five_hour']['left'], view['seven_day']['left']), (98.0, 80.0))
        self.assertIn('98% left · resets 59m', m.limit_text(view['five_hour'], state, time.time()))
        self.assertNotIn('SECRET', (m.DATA / 'limits.json').read_text())

    def test_rate_limit_backs_off_and_falls_back_to_cache(self):
        import io, urllib.error
        cache = {'fetchedAtMs': time.time() * 1000 - 60000, 'accountUuid': 'u1', 'utilization': self.api(10.0, 30.0)}
        self.sign_in(cache=cache)
        calls = []
        def limited(token):
            calls.append(1)
            raise urllib.error.HTTPError(m.USAGE_URL, 429, 'slow down', {'retry-after': '900'}, io.BytesIO())
        m.fetch_usage = limited
        state = m.poll_limits()
        self.assertEqual((state['source'], state['error'], len(calls)), ('cache', 'usage API HTTP 429', 1))
        self.assertGreaterEqual(state['backoff_until'] - time.time(), 899)
        m.poll_limits()
        self.assertEqual(len(calls), 1)  # still backing off
        self.assertEqual(m.limit_view(state, time.time())['five_hour']['left'], 90.0)

    def test_expired_token_is_never_used_or_refreshed(self):
        self.sign_in(cache={'fetchedAtMs': 1000, 'accountUuid': 'other-account', 'utilization': self.api(50.0, 50.0)})
        (m.CLAUDE / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'old', 'refreshToken': 'r', 'expiresAt': (time.time() - 10) * 1000}}))
        m.fetch_usage = lambda token: self.fail('expired token used')
        state = m.poll_limits()
        self.assertEqual((state['error'], state.get('windows')), ('no valid Claude login token', None))

    def test_account_switch_discards_old_readings(self):
        self.sign_in(email='pro@example.com', kind='claude_pro')
        m.fetch_usage = lambda token: self.api(70.0, 90.0)
        m.poll_limits()
        self.sign_in(email='team@example.com')
        m.fetch_usage = lambda token: (_ for _ in ()).throw(OSError('offline'))
        state = m.poll_limits()
        self.assertEqual((state['account'], state.get('windows')), ('team@example.com', None))
        self.assertEqual(m.limit_text(None, state, time.time()), 'usage API OSError')

    def test_calibrated_estimate_between_polls_and_rollover(self):
        self.sign_in()
        path = Path(self.tmp.name) / 'proj' / 'chat-a.jsonl'
        path.parent.mkdir()
        def spend(mid, at, output):
            stamp = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(at))
            with open(path, 'a') as f:
                f.write(json.dumps({'type': 'assistant', 'timestamp': stamp, 'message': {'id': mid, 'usage': {
                    'input_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
                    'output_tokens': output}}}) + '\n')
        now = time.time()
        self.event('UserPromptSubmit', transcript_path=str(path))
        m.fetch_usage = lambda token: self.api(10.0, 20.0)
        m.poll_limits(now - 600)
        spend('a', now - 300, 2000)   # 10k weighted tokens
        m.session_usage(m.refresh_sessions()[0])
        m.fetch_usage = lambda token: self.api(14.0, 21.0)
        state = m.poll_limits(now - 60)
        self.assertAlmostEqual(state['rates']['five_hour'], 4 / 10000)
        self.assertNotIn('seven_day', state['rates'])  # one point is too coarse to calibrate
        spend('b', now - 30, 1000)    # 5k weighted tokens since the last reading
        m.session_usage(m.refresh_sessions()[0])
        view = m.limit_view(state, now)
        self.assertAlmostEqual(view['five_hour']['left'], 84.0)
        self.assertTrue(view['five_hour']['estimated'])
        self.assertTrue(m.limit_text(view['five_hour'], state, now).startswith('~84%'))
        self.assertEqual(view['seven_day']['left'], 79.0)
        later = now + 7200  # past the 5h reset
        self.assertEqual((m.limit_view(state, later)['five_hour']['left'], m.limit_view(state, later)['five_hour']['resets']),
                         (100.0, None))
        self.assertEqual(m.duration(86400 * 2 + 7200), '2d 2h')

    def codex_rollout(self, sid, source='user', when=None):
        when = when or time.time()
        day = m.CODEX / 'sessions' / time.strftime('%Y/%m/%d', time.localtime(when))
        day.mkdir(parents=True, exist_ok=True)
        path = day / f'rollout-x-{sid}.jsonl'
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(when))
        limits = {'limit_id': 'codex', 'plan_type': 'plus',
                  'primary': {'used_percent': 68.0, 'window_minutes': 300, 'resets_at': int(when) + 3600},
                  'secondary': {'used_percent': 26.0, 'window_minutes': 10080, 'resets_at': int(when) + 86400}}
        usage = {'input_tokens': 1000, 'cached_input_tokens': 800, 'cache_write_input_tokens': 0, 'output_tokens': 50}
        records = [
            {'timestamp': stamp, 'type': 'session_meta', 'payload': {'id': sid, 'thread_source': source,
                                                                    'base_instructions': 'secret ' * 50}},
            {'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'task_started', 'model_context_window': 258400}},
            {'timestamp': stamp, 'type': 'turn_context', 'payload': {'model': 'gpt-test', 'cwd': '/work/demo'}},
            {'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'message', 'content': 'private words'}},
            {'timestamp': stamp, 'type': 'token_usage_record', 'payload': {'response_id': 'r1', 'usage': usage}},
            {'timestamp': stamp, 'type': 'token_usage_record', 'payload': {'response_id': 'r1', 'usage': usage}},
            {'timestamp': stamp, 'type': 'token_usage_record', 'payload': {'response_id': 'r2', 'usage': dict(usage, input_tokens=1500)}},
            {'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': dict(limits, limit_id='premium')}},
            {'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': limits}},
            {'timestamp': stamp, 'type': 'compacted', 'payload': {'message': ''}},
        ]
        path.write_text(''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in records))
        return path

    def codex_event(self, event, sid='codex-a', **extra):
        if 'transcript_path' not in extra:  # real chats always report their rollout
            path = next(iter(sorted(m.CODEX.glob(f'sessions/**/rollout-x-{sid}.jsonl'))), None)
            extra['transcript_path'] = str(path or self.codex_rollout(sid))
        m.record(dict(session_id=sid, hook_event_name=event, cwd='/work/demo', **extra), self.owner, provider='codex')

    def test_rolloutless_codex_sessions_are_ignored_and_missed_stops_caught(self):
        # An approval's Guardian review: hooks fire with no rollout and never send Stop.
        self.codex_event('UserPromptSubmit', sid='guardian', transcript_path=None)
        self.assertEqual(m.refresh_sessions(), [])
        with m.contextlib.closing(m.connect()) as db, db:  # a card left over from before this fix
            m.save(db, dict(m.blank('stale'), provider='codex', state='thinking', created=time.time(),
                            updated=time.time(), source='hook', cwd=''))
        self.assertEqual(m.refresh_sessions(), [])
        # A chat whose rollout is not held open (VS Code) that missed its Stop hook.
        chat = self.codex_rollout('codex-v')
        self.codex_event('UserPromptSubmit', sid='codex-v', transcript_path=str(chat))
        self.assertEqual(m.refresh_sessions()[0]['state'], 'thinking')
        later = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(time.time() + 2))
        with open(chat, 'a') as f:
            f.write(json.dumps({'timestamp': later, 'type': 'event_msg', 'payload': {'type': 'task_complete'}},
                               separators=(',', ':')) + '\n')
        row = m.refresh_sessions()[0]
        self.assertEqual((row['state'], row['source']), ('ready', 'hook'))
        self.assertIsNotNone(row['last_turn'])

    def test_codex_chats_get_cards_and_internal_threads_do_not(self):
        chat = self.codex_rollout('codex-a')
        self.codex_event('SessionStart', source='startup', transcript_path=str(chat))
        self.codex_event('UserPromptSubmit', transcript_path=str(chat))
        self.codex_event('PreToolUse', tool_name='Bash', tool_use_id='t1', tool_input={'command': 'ls'})
        row = m.refresh_sessions()[0]
        self.assertEqual((row['provider'], row['state'], row['tool']), ('codex', 'working', 'Bash'))
        self.codex_event('Interrupt')
        row = m.refresh_sessions()[0]
        self.assertEqual(row['state'], 'idle')
        self.assertIsNotNone(row['last_turn'])
        reviewer = self.codex_rollout('codex-review', source='guardian_review')
        self.codex_event('UserPromptSubmit', sid='codex-review', transcript_path=str(reviewer))
        self.assertEqual([r['id'] for r in m.refresh_sessions()], ['codex-a'])
        self.event('Interrupt', sid='claude-x')  # Claude Code has no Interrupt hook
        self.assertEqual([r['id'] for r in m.refresh_sessions()], ['codex-a'])
        self.codex_event('SessionEnd', reason='other')
        self.assertEqual(m.refresh_sessions(), [])

    def test_loaded_codex_threads_are_detected_without_hooks(self):
        # Codex app servers hold a loaded thread's rollout open; this test process plays that role.
        m.CODEX_COMM = Path('/proc/self/comm').read_text().strip()
        chat, review = self.codex_rollout('codex-a'), self.codex_rollout('codex-review', source='guardian_review')
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
        with open(chat, 'a') as f:
            f.write(json.dumps({'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'task_started'}}) + '\n')
        with open(chat, 'rb'), open(review, 'rb'):
            rows = m.refresh_sessions()
            self.assertEqual([(r['id'], r['provider'], r['source'], r['state'], r['cwd']) for r in rows],
                             [('codex-a', 'codex', 'metadata', 'thinking', '')])
            self.assertTrue(m.status_text(rows[0]).endswith('detected'))
            with open(chat, 'a') as f:  # a long turn: megabytes of tool output after task_started
                for _ in range(7):
                    f.write(json.dumps({'timestamp': stamp, 'type': 'response_item',
                                        'payload': {'type': 'custom_tool_call_output', 'output': 'x' * (1 << 20)}}) + '\n')
            m._codex_turns.clear()  # first sight of the thread, as after a monitor restart
            self.assertEqual(m.refresh_sessions()[0]['state'], 'thinking')
            with open(chat, 'a') as f:
                f.write(json.dumps({'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'task_complete'}}) + '\n')
            self.assertEqual(m.refresh_sessions()[0]['state'], 'ready')
            avatar = m.refresh_sessions()[0]['avatar']
            # A hook under another id for the same rollout takes the detected card over.
            self.codex_event('UserPromptSubmit', sid='codex-a-hook', transcript_path=str(chat))
            rows = m.refresh_sessions()
            self.assertEqual([(r['id'], r['source'], r['state'], r['avatar']) for r in rows],
                             [('codex-a-hook', 'hook', 'thinking', avatar)])
            # Ended (SessionEnd) while this process held it: stays gone. Ended before this process
            # started, i.e. an app restart reopened it: comes back.
            self.codex_event('SessionEnd', sid='codex-a-hook', transcript_path=str(chat))
            with m.contextlib.closing(m.connect()) as db, db:
                db.execute("DELETE FROM sessions")
                db.execute("INSERT OR REPLACE INTO ended VALUES ('codex-a', ?)", (time.time(),))
            self.assertEqual(m.refresh_sessions(), [])
            opened = m.started_at(self.owner[1])
            with m.contextlib.closing(m.connect()) as db, db:
                db.execute("UPDATE ended SET ended=? WHERE id='codex-a'", (opened - 60,))
            self.assertEqual([r['id'] for r in m.refresh_sessions()], ['codex-a'])
        m._codex_scan[0] = 0
        with m.contextlib.closing(m.connect()) as db, db:
            db.execute("UPDATE sessions SET source='metadata'")
        self.assertEqual(m.refresh_sessions(), [])  # the thread was unloaded

    def test_idle_codex_chat_expires_but_waiting_one_stays(self):
        self.codex_event('Stop', sid='codex-idle')
        self.codex_event('PermissionRequest', sid='codex-ask', tool_name='Bash')
        with m.contextlib.closing(m.connect()) as db, db:
            db.execute('UPDATE sessions SET updated=?', (time.time() - m.CODEX_IDLE - 5,))
        self.assertEqual([r['id'] for r in m.refresh_sessions()], ['codex-ask'])

    def test_codex_usage_title_and_limits_from_local_files(self):
        chat = self.codex_rollout('codex-a')
        (m.CODEX / 'session_index.jsonl').write_text(json.dumps({'id': 'codex-a', 'thread_name': 'Fix  the build'}) + '\n')
        self.codex_event('UserPromptSubmit', transcript_path=str(chat))
        u = m.session_usage(m.refresh_sessions()[0])
        self.assertEqual((u['model'], u['window'], u['requests'], u['compactions']), ('gpt-test', 258400, 2, 1))
        self.assertEqual((u['input'], u['cache_read'], u['output'], u['context']), (2500, 1600, 100, 1500))
        self.assertEqual(u['title'], 'Fix the build')
        self.assertEqual(m._usage, {})  # Codex tokens never feed the Anthropic calibration
        reading = m.codex_limits()
        self.assertEqual((reading['plan'], reading['windows']['primary']['used']), ('plus', 68.0))
        view = m.codex_view(reading, time.time())
        self.assertEqual((view['primary']['left'], view['secondary']['left']), (32.0, 74.0))
        self.assertEqual(m.codex_view(reading, time.time() + 7200)['primary']['resets'], None)
        self.assertEqual((m.window_span(300, '?'), m.window_span(10080, '?'), m.window_span(None, '7d')), ('5h', '7d', '7d'))
        stored = (m.DATA / 'sessions.sqlite3').read_bytes()
        self.assertNotIn(b'private words', stored)
        self.assertNotIn(b'secret', stored)

    def test_codex_installer_roundtrip_and_strict_json_replies(self):
        other = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'notify-other'}]}]}}
        installed = m.merge_codex_hooks(other)
        self.assertEqual(installed, m.merge_codex_hooks(installed))
        self.assertEqual(set(installed['hooks']), set(m.CODEX_EVENTS))
        ours = [h for g in installed['hooks'].values() for x in g for h in x['hooks'] if h['command'] != 'notify-other']
        self.assertTrue(len(ours) == len(m.CODEX_EVENTS) and all(h['timeout'] <= 3 for h in ours))
        self.assertEqual(m.merge_codex_hooks(installed, remove=True), other)
        m.install_codex_hooks()
        path = m.CODEX / 'hooks.json'
        self.assertTrue(json.loads(path.read_text())['hooks']['Interrupt'][0]['hooks'][0]['command'].endswith(' hook codex'))
        m.install_codex_hooks(remove=True)
        self.assertFalse(path.exists())
        import subprocess, sys
        env = dict(os.environ, AGENT_MONITOR_HOME=str(m.DATA), CODEX_HOME=str(m.CODEX))
        m.save_config({'enabled': False, 'avatars': {'Tifa': str(m.ROOT / 'assets/tifa')}})  # no window
        script = str(m.ROOT / 'scripts/monitor.py')
        for event, expected in (('Stop', '{}'), ('Interrupt', '{}'), ('SubagentStop', '{}'), ('UserPromptSubmit', '')):
            body = json.dumps({'session_id': 'codex-b', 'hook_event_name': event, 'cwd': '/w'})
            out = subprocess.run([sys.executable, script, 'hook', 'codex'], input=body, env=env,
                                 capture_output=True, text=True, timeout=20)
            self.assertEqual((out.returncode, out.stdout.strip()), (0, expected))
        out = subprocess.run([sys.executable, script, 'hook'], input=json.dumps(
            {'session_id': 'claude-b', 'hook_event_name': 'Stop', 'cwd': '/w'}), env=env, capture_output=True, text=True)
        self.assertEqual(out.stdout, '')  # Claude Code hooks stay silent

    def test_one_theme_at_a_time_for_busy_unmuted_chats(self):
        rows = [dict(id='a', state='working', turn_started=10, updated=10),
                dict(id='b', state='thinking', turn_started=20, updated=20),
                dict(id='c', state='ready', turn_started=None, updated=30),
                dict(id='d', state='working', turn_started=40, updated=40, muted=1)]
        themes = {'a': 'a.ogg', 'b': 'b.ogg', 'c': 'c.ogg', 'd': 'd.ogg'}
        self.assertEqual(m.pick_theme(rows, None, themes), 'b')  # latest busy, unmuted turn
        self.assertEqual(m.pick_theme(rows, 'a', themes), 'a')   # the playing one keeps going
        self.assertEqual(m.pick_theme(rows, 'c', themes), 'b')   # finished chats stop
        self.assertIsNone(m.pick_theme(rows, None, {'c': 'c.ogg', 'd': 'd.ogg'}))  # no theme, no music

    def test_theme_lookup_set_theme_and_mute_survive_hooks(self):
        folder = Path(self.tmp.name) / 'hero'
        folder.mkdir()
        cfg = {'avatars': {'Hero': str(folder)}}
        self.assertIsNone(m.theme_for('Hero', cfg))
        (folder / 'theme.mp3').write_bytes(b'x')
        self.assertEqual(m.theme_for('Hero', cfg).name, 'theme.mp3')
        song = Path(self.tmp.name) / 'mine.ogg'
        song.write_bytes(b'x')
        m.save_config({'enabled': False, 'avatars': {'Hero': str(folder)}})
        import subprocess, sys
        env = dict(os.environ, AGENT_MONITOR_HOME=str(m.DATA))
        run = lambda *a: subprocess.run([sys.executable, str(m.ROOT / 'scripts/monitor.py'), 'set-theme', *a],
                                        env=env, capture_output=True, text=True)
        self.assertEqual(run('Hero', str(song)).returncode, 0)
        self.assertEqual(m.theme_for('Hero', m.config()), song)
        self.assertNotEqual(run('Nobody', str(song)).returncode, 0)
        self.assertNotEqual(run('Hero', str(folder / 'missing.ogg')).returncode, 0)
        self.assertIn('Its folder still has theme.mp3', run('Hero').stdout)
        self.assertEqual(m.theme_for('Hero', m.config()).name, 'theme.mp3')
        self.event('UserPromptSubmit')
        with m.contextlib.closing(m.connect()) as db, db:
            db.execute('UPDATE sessions SET muted=1')
        self.event('PreToolUse', tool_name='Bash')
        self.assertEqual(m.refresh_sessions()[0]['muted'], 1)

    def test_chimes_are_short_distinct_wavs(self):
        import io, wave
        sounds = {}
        for kind in m.CHIMES:
            with wave.open(io.BytesIO(m.chime_wav(kind))) as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth()), (1, 2))
                self.assertLess(w.getnframes() / w.getframerate(), 1)
                sounds[kind] = w.readframes(w.getnframes())
                self.assertTrue(any(sounds[kind]))
        self.assertEqual(len(set(sounds.values())), len(m.CHIMES))
        self.assertEqual(m.chime_path('ready').read_bytes(), m.chime_wav('ready'))
        for name in ('sound', 'muted'):
            self.assertEqual(m.draw_ui_icon(name, 18).size, (18, 18))
        self.assertNotEqual(m.draw_ui_icon('sound').tobytes(), m.draw_ui_icon('muted').tobytes())
        self.assertNotEqual(m.draw_ui_icon('clock').tobytes(), m.draw_ui_icon('calendar').tobytes())

    RATE_LIMITS = {'rateLimits': {'limitId': 'codex', 'planType': 'plus', 'rateLimitReachedType': None,
                                  'primary': {'usedPercent': 40, 'windowDurationMins': 300, 'resetsAt': 0},
                                  'secondary': {'usedPercent': 63, 'windowDurationMins': 10080, 'resetsAt': 0}},
                   'rateLimitResetCredits': {'availableCount': 1}}

    def fake_codex(self, behaviour):
        """A stand-in Codex app server speaking just enough JSON-RPC: 'ok', 'error', 'crash' or 'hang'."""
        import sys
        limits = json.loads(json.dumps(self.RATE_LIMITS))
        limits['rateLimits']['primary']['resetsAt'] = int(time.time()) + 3600
        limits['rateLimits']['secondary']['resetsAt'] = int(time.time()) + 86400
        path = Path(self.tmp.name) / f'codex-{behaviour}'
        path.write_text(f"""#!{sys.executable}
import json, sys, time
assert sys.argv[1:] == ['app-server']
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get('id') == 1:
        print(json.dumps({{'id': 1, 'result': {{}}}}), flush=True)
    elif msg.get('id') == 2:
        behaviour = {behaviour!r}
        if behaviour == 'crash':
            sys.exit(3)
        if behaviour == 'hang':
            time.sleep(30)
        reply = {{'id': 2, 'error': {{'message': 'not signed in'}}}} if behaviour == 'error' else {{'id': 2, 'result': {limits!r}}}
        print(json.dumps(reply), flush=True)
""")
        path.chmod(0o755)
        return str(path)

    def test_codex_limits_polled_through_app_server(self):
        m.save_config({'enabled': False, 'avatars': {}, 'codex_binary': self.fake_codex('ok')})
        self.assertEqual(m.codex_binary(), m.config()['codex_binary'])
        state = m.poll_codex_limits()
        self.assertEqual((state['source'], state['plan'], state['reached'], state['reset_credits'], state['error']),
                         ('api', 'plus', False, 1, None))
        self.assertEqual({k: w['used'] for k, w in state['windows'].items()}, {'primary': 40.0, 'secondary': 63.0})
        reading = m.codex_limits()
        self.assertEqual(reading['source'], 'api')
        view = m.codex_view(reading, time.time())
        self.assertTrue(m.limit_text(view['primary'], reading, time.time()).startswith('60% left · resets 59m'))
        # Codex logging a newer reading with a response wins, keeping the polled reset-credit count.
        self.codex_rollout('codex-a', when=time.time() + 5)
        reading = m.codex_limits()
        self.assertEqual((reading['source'], reading['windows']['primary']['used'], reading['reset_credits']), ('log', 68.0, 1))

    def test_codex_poll_failures_back_off_and_keep_last_reading(self):
        m.save_config({'enabled': False, 'avatars': {}, 'codex_binary': self.fake_codex('ok')})
        m.poll_codex_limits()
        for behaviour, error in (('error', 'Codex usage: not signed in'), ('crash', 'Codex usage: Codex app server exited')):
            m.save_config({'enabled': False, 'avatars': {}, 'codex_binary': self.fake_codex(behaviour)})
            state = m.poll_codex_limits(time.time() + 10 ** 6 * (behaviour == 'crash'))  # past the first backoff
            self.assertEqual(state['error'], error)
            self.assertEqual(state['windows']['primary']['used'], 40.0)  # the last good reading stays
        self.assertEqual(state['failures'], 2)
        self.assertGreater(state['backoff_until'], time.time() + 10 ** 6)
        with self.assertRaises(TimeoutError):
            m.fetch_codex_limits(self.fake_codex('hang'), timeout=1)
        self.assertEqual(m.parse_codex_limits(dict(self.RATE_LIMITS, rateLimits=dict(
            self.RATE_LIMITS['rateLimits'], rateLimitReachedType='rate_limit_reached')), 1.0)['reached'], True)

    def test_codex_chat_name_comes_from_codex_state(self):
        import sqlite3
        m.CODEX.mkdir(parents=True, exist_ok=True)
        (m.CODEX / 'session_index.jsonl').write_text(json.dumps({'id': 'c', 'thread_name': 'From the index'}) + '\n')
        with m.contextlib.closing(sqlite3.connect(m.CODEX / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, preview TEXT)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?)', [
                ('a', 'Find G1  Gripper Files', 'search online for any g1 gripper', 'x'),
                ('b', None, '[@Pets](plugin://work-pets@openai-curated-remote) make me a pet', 'x'),
                ('c', None, '', 'x')])
        self.assertEqual(m.codex_title('a'), 'Find G1 Gripper Files')   # the sidebar name wins
        self.assertEqual(m.codex_title('b'), '@Pets make me a pet')     # else the first message, links unwrapped
        self.assertEqual(m.codex_title('c'), 'From the index')          # else session_index.jsonl
        self.assertIsNone(m.codex_title('missing'))
        with m.contextlib.closing(sqlite3.connect(m.CODEX / 'state_5.sqlite')) as db, db:
            db.execute("UPDATE threads SET name='Renamed' WHERE id='b'")
        self.assertEqual(m.codex_title('b'), '@Pets make me a pet')     # cached for a few seconds
        self.assertEqual(m.codex_state_title('b', time.time() + 6), 'Renamed')


if __name__ == '__main__':
    unittest.main()
