"""Real Tk smoke check; uses only temporary monitor state and synthetic chats."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import json
import tkinter as tk

spec = importlib.util.spec_from_file_location('monitor', Path(__file__).resolve().parents[1] / 'scripts/monitor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def main():
    with tempfile.TemporaryDirectory(prefix='agent-monitor-gui-') as temp:
        m.DATA = Path(temp) / 'state'
        m.CLAUDE = Path(temp) / 'claude'
        m.CLAUDE_JSON = Path(temp) / '.claude.json'
        m.CODEX = Path(temp) / 'codex'
        day = m.CODEX / 'sessions' / time.strftime('%Y/%m/%d')
        day.mkdir(parents=True)
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
        (day / 'rollout-demo-codex.jsonl').write_text(json.dumps({'timestamp': now_iso, 'type': 'session_meta',
            'payload': {'id': 'demo-codex', 'thread_source': 'user'}}) + '\n' + json.dumps({
            'timestamp': now_iso, 'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': {
                'limit_id': 'codex', 'plan_type': 'plus',
                'primary': {'used_percent': 68.0, 'window_minutes': 300, 'resets_at': int(time.time()) + 9000},
                'secondary': {'used_percent': 26.0, 'window_minutes': 10080, 'resets_at': int(time.time()) + 300000}}}},
            separators=(',', ':')) + '\n')
        reset = lambda dt: time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(time.time() + dt))
        m.CLAUDE_JSON.write_text(json.dumps({
            'oauthAccount': {'emailAddress': 'demo@example.com', 'organizationName': 'Demo Org',
                             'organizationType': 'claude_team', 'accountUuid': 'u',
                             'userRateLimitTier': 'default_claude_max_5x'},
            'cachedUsageUtilization': {'fetchedAtMs': time.time() * 1000, 'accountUuid': 'u', 'utilization': {
                'five_hour': {'utilization': 35, 'resets_at': reset(8000)},
                'seven_day': {'utilization': 72, 'resets_at': reset(200000)}}}}))
        owner = (os.getpid(), m.process_info(os.getpid())['start'])
        def event(sid, name, **extra):
            m.record(dict(session_id=sid, cwd='/demo/preview', hook_event_name=name, **extra), owner)
        demos = ('demo-working', 'demo-reading', 'demo-thinking', 'demo-waiting', 'demo-ready', 'demo-failed')
        for sid in demos:
            event(sid, 'UserPromptSubmit')
        event('demo-working', 'PreToolUse', tool_name='Bash')
        event('demo-reading', 'PreToolUse', tool_name='Grep')
        event('demo-waiting', 'PermissionRequest')
        event('demo-ready', 'Stop')
        event('demo-failed', 'StopFailure')
        m.record(dict(session_id='demo-codex', cwd='/demo/preview', hook_event_name='PreToolUse', tool_name='Bash',
                      transcript_path=str(day / 'rollout-demo-codex.jsonl')), owner, provider='codex')
        song = Path(temp) / 'tifa-theme.ogg'
        song.write_bytes(b'not really audio')  # never played: m.play is replaced below
        m.save_config({'enabled': True, 'verbose': True, 'avatars': {'Tifa': str(m.ROOT / 'assets/tifa')},
                       'themes': {'Tifa': str(song)}})
        plays = []
        class FakePlayer:
            def __init__(self, command):
                self.command, self.code = command, None
                plays.append(self)
            def poll(self):
                return self.code
            def terminate(self):
                self.code = -15
        m.play = FakePlayer
        m.fetch_codex_limits = lambda binary, timeout=20: {'rateLimits': {  # never the real app server
            'planType': 'plus', 'rateLimitReachedType': 'rate_limit_reached',
            'primary': {'usedPercent': 100, 'windowDurationMins': 300, 'resetsAt': int(time.time()) + 9000},
            'secondary': {'usedPercent': 26, 'windowDurationMins': 10080, 'resetsAt': int(time.time()) + 300000}},
            'rateLimitResetCredits': {'availableCount': 1}}
        original = tk.Tk
        failures = []
        def labels(root):
            found = []
            def walk(w):
                if isinstance(w, tk.Label):
                    found.append(w.cget('text'))
                for child in w.winfo_children():
                    walk(child)
            walk(root)
            return found
        def widgets(root):
            found = []
            def walk(w):
                found.append(w)
                for child in w.winfo_children():
                    walk(child)
            walk(root)
            return found
        def make_root(*args, **kwargs):
            root = original(*args, **kwargs)
            def check_initial():
                try:
                    texts = labels(root)
                    assert '7 chats' in texts, texts
                    # The (stubbed) app-server poll is newer than the logged reading, so it wins.
                    assert 'OPENAI · Codex Plus · limit reached · 1 free reset' in texts, texts
                    assert any(t.startswith('5h · 0% left · resets 2h') for t in texts), texts
                    assert any(t.startswith('7d · 74% left · resets 3d') for t in texts), texts
                    assert 'preview\ndemo-cod' in texts and 'preview\ndemo-wor' in texts, texts  # one layout for both
                    assert texts.count('CODEX') == 1 and texts.count('CLAUDE') == len(demos), texts
                    for state in ('Working · Bash','Reading · Grep','Thinking','Needs input','Ready','Error'):
                        assert state in texts, texts
                    assert any(t.startswith('session ') and 'prompts' in t for t in texts), texts
                    assert 'ANTHROPIC · Demo Org · Team (Max 5x) · demo@example.com' in texts, texts
                    assert any(t.startswith('5h · 65% left · resets 2h') for t in texts), texts
                    assert any(t.startswith('7d · 28% left · resets 2d') for t in texts), texts
                    assert not any('no model calls' in t for t in texts), texts  # footer moved into help
                    named = {str(w).rsplit('.', 1)[-1]: w for w in widgets(root)}
                    named['details'].invoke(); named['details'].invoke()  # icon toggle round-trips
                    root.after(10, check_sound)
                except BaseException as e:
                    failures.append(e)
            def check_sound():
                try:
                    def themes():
                        return [p for p in plays if '-loop' in p.command]
                    def lit():
                        return [w for w in widgets(root) if str(w).endswith('.mute') and w.cget('bg') == '#4a6fa5']
                    assert len(themes()) == 1 and themes()[0].command[-1] == str(song), [p.command for p in plays]
                    assert len(lit()) == 1  # exactly one card shows its theme playing
                    first = themes()[0]
                    lit()[0].invoke()  # muting that card hands the theme to the next busy chat
                    assert first.code is not None and len(themes()) == 2 and themes()[1].code is None
                    assert len(lit()) == 1
                    named = {str(w).rsplit('.', 1)[-1]: w for w in widgets(root)}
                    named['sound'].invoke()  # master mute stops everything
                    assert themes()[1].code is not None and not lit() and m.config()['sound'] is False
                    named['sound'].invoke()
                    assert len(themes()) == 3 and m.config()['sound'] is True
                    event('demo-thinking', 'Stop')
                    root.after(900, check_chime)
                except BaseException as e:
                    failures.append(e)
            def check_chime():
                try:
                    chimes = [p.command for p in plays if '-loop' not in p.command]
                    assert len(chimes) == 1 and chimes[0][-1].endswith('ready-v1.wav'), chimes
                    named = {str(w).rsplit('.', 1)[-1]: w for w in widgets(root)}
                    named['help'].invoke()
                    root.after(400, check_help)
                except BaseException as e:
                    failures.append(e)
            def check_help():
                try:
                    popup = root.nametowidget('help_popup')
                    canvas = popup.winfo_children()[0]
                    texts = [canvas.itemcget(i, 'text') for i in canvas.find_all() if canvas.type(i) == 'text']
                    for expected in ('AGENT MONITOR', 'STATUS', 'PRIVACY', '▶ OK'):
                        assert expected in texts, texts
                    assert any(t.startswith('Needs input') for t in texts), texts
                    assert len([i for i in canvas.find_all() if canvas.type(i) == 'image']) == len(m.LABELS)
                    if len(sys.argv) > 2:
                        from PIL import ImageGrab
                        popup.update()
                        ImageGrab.grab(bbox=(popup.winfo_rootx(), popup.winfo_rooty(),
                                             popup.winfo_rootx() + popup.winfo_width(),
                                             popup.winfo_rooty() + popup.winfo_height())).save(sys.argv[2])
                    named = {str(w).rsplit('.', 1)[-1]: w for w in widgets(root)}
                    named['help'].invoke()  # the ? button closes it again
                    root.update_idletasks()
                    assert not any(str(w).endswith('help_popup') for w in root.winfo_children())
                    # An avatar registered while the window is open shows up in the picker.
                    c = m.config(); c['avatars']['Twin'] = c['avatars']['Tifa']; m.save_config(c)
                    for name in ('Rinoa','Yuna'):
                        c['avatars'][name] = str(m.ROOT / 'assets' / name.lower())
                    m.save_config(c)
                    buttons = [w for w in widgets(root) if isinstance(w, tk.Button) and w.cget('text') == 'Tifa  ▾']
                    assert len(buttons) == len(demos) + 1, [w.cget('text') for w in widgets(root) if isinstance(w, tk.Button)]
                    buttons[0].invoke()
                    root.after(300, check_picker)
                except BaseException as e:
                    failures.append(e)
            def check_picker():
                try:
                    popup = root.nametowidget('avatar_popup')
                    canvas = popup.winfo_children()[0]
                    texts = [canvas.itemcget(i, 'text') for i in canvas.find_all() if canvas.type(i) == 'text']
                    for expected in ('CHOOSE AVATAR', 'Tifa', 'Twin', 'Rinoa', 'Yuna', '▶'):
                        assert expected in texts, texts
                    assert len([i for i in canvas.find_all() if canvas.type(i) == 'image']) == 4
                    if len(sys.argv) > 3:
                        from PIL import ImageGrab
                        popup.update()
                        ImageGrab.grab(bbox=(popup.winfo_rootx(), popup.winfo_rooty(),
                                             popup.winfo_rootx() + popup.winfo_width(),
                                             popup.winfo_rooty() + popup.winfo_height())).save(sys.argv[3])
                    twin = next(i for i in canvas.find_all() if canvas.type(i) == 'text' and canvas.itemcget(i, 'text') == 'Twin')
                    canvas.event_generate('<Button-1>', x=int(canvas.coords(twin)[0]) + 5, y=int(canvas.coords(twin)[1]))
                    root.update_idletasks()
                    assert not any(str(w).endswith('avatar_popup') for w in root.winfo_children())
                    with m.contextlib.closing(m.connect()) as db:
                        assert sorted(r[0] for r in db.execute('SELECT avatar FROM sessions')).count('Twin') == 1
                        db.execute('UPDATE sessions SET avatar=? WHERE id=?', ('Rinoa','demo-reading'))
                        db.execute('UPDATE sessions SET avatar=? WHERE id=?', ('Yuna','demo-thinking'))
                        db.commit()
                    root.geometry('1040x900+60+60')
                    root.update_idletasks()
                    root.after(1000, capture_and_remove)
                except BaseException as e:
                    failures.append(e)
            def capture_and_remove():
                try:
                    sprites = [w for w in widgets(root) if isinstance(w,tk.Label) and w.cget('image') and
                               int(root.tk.call('image','width',w.cget('image'))) == 168]
                    assert len(sprites) == len(demos) + 1  # plus the Codex card
                    assert all(int(root.tk.call('image','height',w.cget('image'))) == 126 for w in sprites)
                    if len(sys.argv) > 1:
                        from PIL import ImageGrab
                        root.update()
                        box = (root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx()+root.winfo_width(), root.winfo_rooty()+root.winfo_height())
                        ImageGrab.grab(bbox=box).save(sys.argv[1])
                    for sid in demos + ('demo-codex',):
                        event(sid, 'SessionEnd')
                    root.after(900, check_empty)
                except BaseException as e:
                    failures.append(e)
            def check_empty():
                try:
                    assert '0 chats' in labels(root), labels(root)
                except BaseException as e:
                    failures.append(e)
                finally:
                    root.destroy()
            def watchdog():
                if root.winfo_exists():
                    failures.append(AssertionError('timed out: a step never reached check_empty/root.destroy()'))
                    root.destroy()
            root.after(1500, check_initial)
            root.after(15000, watchdog)
            return root
        tk.Tk = make_root
        m.run_ui()
        if failures:
            raise failures[0]
        assert not (m.DATA / 'ui.json').exists()
        print('GUI smoke passed: states, verbose details, usage gauges, help popup, avatar picker, Codex card, per-provider usage bars, themes, mutes, chimes, resizing, automatic removal, clean shutdown.')


if __name__ == '__main__':
    main()
