#!/usr/bin/env python3
"""Local Claude session avatars. Standard library + Tk/Pillow for display only."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import random
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('AGENT_MONITOR_HOME', Path.home() / '.local/state/agent-monitor'))
CLAUDE = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
CLAUDE_JSON = CLAUDE / '.claude.json' if 'CLAUDE_CONFIG_DIR' in os.environ else Path.home() / '.claude.json'
CODEX = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
EVENTS = ('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PermissionRequest', 'PermissionDenied',
          'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Notification',
          'Stop', 'StopFailure', 'Elicitation', 'ElicitationResult',
          'SubagentStart', 'SubagentStop', 'PreCompact', 'PostCompact')
# Codex hooks share Claude Code's payload fields; Interrupt is Codex-only.
CODEX_EVENTS = ('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PermissionRequest', 'PreToolUse',
                'PostToolUse', 'PreCompact', 'PostCompact', 'SubagentStart', 'SubagentStop', 'Stop', 'Interrupt')
CODEX_JSON_EVENTS = {'Stop', 'SubagentStop', 'Interrupt'}  # Codex fails the turn on non-JSON stdout here
CODEX_INTERNAL = {'guardian_review', 'subagent'}  # approval reviewers and child threads, not chats
CODEX_IDLE = 2 * 3600  # Codex app chats share one process, so a closed chat expires after this
CODEX_COMM = 'codex'  # process name prefix of Codex app servers, which hold loaded threads' rollouts open
PROVIDERS = {'claude': 'Claude', 'codex': 'Codex'}
TAG_COLORS = {'claude': '#e8b48f', 'codex': '#9fd3c7'}  # the small provider tag in a card's corner
# Each animation lists GIF names in preference order; the first five are required for any avatar.
ANIMATIONS = {'idle': ('idle',), 'working': ('running',), 'waiting': ('waiting',),
              'ready': ('waving',), 'failed': ('failed',),
              'thinking': ('review', 'idle'), 'reading': ('review', 'running'),
              'delegating': ('running-right', 'running'), 'compacting': ('idle-jump-idle', 'idle'),
              'background': ('look-loop', 'waiting'), 'celebrate': ('jumping', 'waving')}
REQUIRED = ('idle', 'running', 'waving', 'waiting', 'failed')
LABELS = {'idle': 'Idle', 'thinking': 'Thinking', 'reading': 'Reading', 'working': 'Working',
          'delegating': 'Delegating', 'compacting': 'Compacting', 'waiting': 'Needs input',
          'ready': 'Ready', 'background': 'Background', 'failed': 'Error'}
COLORS = {'idle': '#a6b0c3', 'thinking': '#b9a8ff', 'reading': '#7fd4e8', 'working': '#8bb6ff',
          'delegating': '#9fc2ff', 'compacting': '#d6b8ff', 'waiting': '#ffcf70',
          'ready': '#84e0bb', 'background': '#9fd3c7', 'failed': '#ff919b'}
CARD_BACKDROPS = {'field': '#1e3035', 'battle': '#332d36', 'boss': '#382733'}
SCENE_INTERVALS = {'working': (8, 14), 'background': (25, 45)}
BUSY = {'thinking', 'reading', 'working', 'delegating', 'compacting'}
READ_TOOLS = {'Read', 'Grep', 'Glob', 'LS', 'WebFetch', 'WebSearch', 'NotebookRead', 'ToolSearch',
              'TaskList', 'TaskGet', 'TaskOutput', 'ListMcpResourcesTool', 'ReadMcpResourceTool'}
AGENT_TOOLS = {'Agent', 'Task'}
WAIT_TOOLS = {'AskUserQuestion', 'ExitPlanMode'}
JOB_TOOLS = {'Monitor'}  # always background; other tools count only with run_in_background
JOB_TTL = 12 * 3600  # forget a background job whose completion notice never appears
NOTICE = re.compile(rb'<tool-use-id>([A-Za-z0-9_-]{1,100})</tool-use-id>.*?<status>([a-z_]{1,20})</status>', re.S)
FLASH_SECONDS = {'failed': 2.5, 'celebrate': 1.6}
READY_WAVE = 60  # After this long, a ready card relaxes to the idle animation.
COLUMNS = {'id': 'TEXT PRIMARY KEY', 'cwd': 'TEXT', 'pid': 'INTEGER', 'proc_start': 'TEXT',
           'state': 'TEXT', 'updated': 'REAL', 'source': 'TEXT', 'avatar': 'TEXT', 'created': 'REAL',
           'started': 'REAL', 'turn_started': 'REAL', 'last_turn': 'REAL', 'busy': 'REAL DEFAULT 0',
           'tool': 'TEXT', 'tools': 'INTEGER DEFAULT 0', 'tool_errors': 'INTEGER DEFAULT 0',
           'prompts': 'INTEGER DEFAULT 0', 'subagents': "TEXT DEFAULT '{}'", 'flash': 'TEXT',
           'flash_until': 'REAL', 'transcript': 'TEXT', 'name': 'TEXT', 'mode': 'TEXT',
           'jobs': "TEXT DEFAULT '{}'", 'provider': 'TEXT', 'muted': 'INTEGER DEFAULT 0',
           'waiters': "TEXT DEFAULT '[]'", 'turn_tools': 'INTEGER DEFAULT 0', 'turn_errors': 'INTEGER DEFAULT 0'}
USAGE_KEYS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
# Relative cost of each USAGE_KEYS kind; calibration against real readings absorbs the absolute scale.
TOKEN_WEIGHTS = (1, 1.25, 0.1, 5)
USAGE_URL = 'https://api.anthropic.com/api/oauth/usage?skip_spend=1'
POLL_SECONDS, POLL_MAX = 300, 3600
WINDOWS = (('five_hour', '5h'), ('seven_day', '7d'))
PLANS = {'claude_team': 'Team', 'claude_pro': 'Pro', 'claude_max': 'Max', 'claude_enterprise': 'Enterprise'}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2)
            f.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def bundled_avatars():
    """Every assets/<name> directory that has an animation profile or an idle GIF."""
    found = sorted(d for d in (ROOT / 'assets').glob('*/') if (d / 'animations.json').exists() or (d / 'idle.gif').exists())
    return {d.name.title(): str(d) for d in found} or {'Tifa': str(ROOT / 'assets/tifa')}


def config():
    try:
        return json.loads((DATA / 'config.json').read_text())
    except FileNotFoundError:
        return {'enabled': True, 'verbose': True, 'avatars': bundled_avatars()}


def save_config(value):
    atomic_json(DATA / 'config.json', value)


def connect():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(DATA / 'sessions.sqlite3', timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS sessions (%s)' % ', '.join(f'{k} {v}' for k, v in COLUMNS.items()))
    db.execute('CREATE TABLE IF NOT EXISTS ended (id TEXT PRIMARY KEY, ended REAL)')
    have = {r[1] for r in db.execute('PRAGMA table_info(sessions)')}
    for name in COLUMNS.keys() - have:
        with contextlib.suppress(sqlite3.OperationalError):  # a concurrent hook added it first
            db.execute(f'ALTER TABLE sessions ADD COLUMN {name} {COLUMNS[name]}')
    db.commit()
    return db


def save(db, row):
    cols = [c for c in COLUMNS if c in row]
    db.execute(f'INSERT OR REPLACE INTO sessions ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
               [row[c] for c in cols])


def blank(sid):
    return dict({c: None for c in COLUMNS}, id=sid, busy=0, tools=0, tool_errors=0, prompts=0,
                subagents='{}', jobs='{}', waiters='[]')


def process_info(pid):
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return {'ppid': int(fields[1]), 'start': fields[19], 'state': fields[0]}
    except (OSError, ValueError, IndexError):
        return None


def started_at(start):
    """Epoch seconds of a process start time as /proc reports it (clock ticks after boot)."""
    try:
        boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                    if line.startswith('btime '))
        return boot + int(start) / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, StopIteration):
        return None


def alive(pid, start):
    p = process_info(pid)
    return bool(p and p['state'] != 'Z' and str(start) == p['start'])


def agent_parent(prefix='claude'):
    pid = os.getppid()
    for _ in range(12):
        p = process_info(pid)
        if not p:
            break
        try:
            exe = Path(f'/proc/{pid}/exe').resolve().name
            comm = Path(f'/proc/{pid}/comm').read_text().strip()
            if exe.startswith(prefix) or comm == prefix:
                return pid, p['start']
        except OSError:
            pass
        if p['ppid'] <= 1:
            break
        pid = p['ppid']
    return None, None


def tool_state(name):
    if name in WAIT_TOOLS:
        return 'waiting'
    if name in AGENT_TOOLS:
        return 'delegating'
    if name in READ_TOOLS:
        return 'reading'
    return 'working'


def state_for(event, payload, previous='idle'):
    if event == 'SessionStart':
        return previous if payload.get('source') == 'compact' else 'idle'
    if event in ('PermissionRequest', 'Elicitation'):
        return 'waiting'
    if event == 'Notification':
        kind = payload.get('notification_type')
        if kind in ('permission_prompt', 'elicitation_dialog', 'elicitation_url_dialog'):
            return 'waiting'
        if kind == 'idle_prompt':
            return previous if previous in ('failed', 'idle') else 'ready'
        return previous
    if event == 'StopFailure':
        return 'failed'
    if event == 'Stop':
        return 'ready'
    if event == 'Interrupt':
        return 'idle'
    if event == 'PreCompact':
        return 'compacting'
    if event == 'PostToolUseFailure' and payload.get('is_interrupt'):
        return 'idle'
    if payload.get('agent_id'):
        # Subagent traffic arrives under the parent session while the parent waits on it.
        if event in ('SubagentStart', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'ElicitationResult',
                     'PermissionDenied'):
            return 'delegating' if previous in BUSY or previous == 'waiting' else previous
        return previous
    if event == 'PreToolUse':
        return tool_state(payload.get('tool_name'))
    if event in ('UserPromptSubmit', 'PostToolUse', 'PostToolUseFailure', 'ElicitationResult',
                 'PermissionDenied', 'PostCompact'):
        return 'thinking'
    return previous


def end_turn(row, now):
    if row.get('turn_started'):
        row['last_turn'] = max(0, now - row['turn_started'])
        row['busy'] = (row.get('busy') or 0) + row['last_turn']
        row['turn_started'] = None
    row.update(tool=None, turn_tools=0, turn_errors=0)


def apply(row, event, payload, now):
    """Advance one session row for a hook event; stores names and counts, never content."""
    sub = payload.get('agent_id')
    agents = json.loads(row.get('subagents') or '{}')
    jobs = json.loads(row.get('jobs') or '{}')
    use_id = payload.get('tool_use_id') if isinstance(payload.get('tool_use_id'), str) else None
    if event == 'UserPromptSubmit':
        row['prompts'] = (row.get('prompts') or 0) + 1
        if not row.get('turn_started'):
            row['turn_started'] = now
    elif event == 'PreToolUse':
        row['tools'] = (row.get('tools') or 0) + 1
        row['turn_tools'] = (row.get('turn_tools') or 0) + 1
        name = payload.get('tool_name')
        if not sub and isinstance(name, str):
            row['tool'] = name[:60]
        args = payload.get('tool_input') if isinstance(payload.get('tool_input'), dict) else {}
        # Only the boolean flag is inspected; background agents are tracked by SubagentStart.
        if not sub and use_id and name not in AGENT_TOOLS and (name in JOB_TOOLS or args.get('run_in_background') is True):
            jobs[use_id[:100]] = now
    elif event == 'PostToolUseFailure' and not payload.get('is_interrupt'):
        row['tool_errors'] = (row.get('tool_errors') or 0) + 1
        row['turn_errors'] = (row.get('turn_errors') or 0) + 1
        row['flash'], row['flash_until'] = 'failed', now + FLASH_SECONDS['failed']
    elif event == 'SubagentStart' and isinstance(sub, str):
        agents[sub[:80]] = now
    elif event == 'SubagentStop' and isinstance(sub, str):
        agents.pop(sub[:80], None)
    elif event == 'Stop':
        row['flash'], row['flash_until'] = 'celebrate', now + FLASH_SECONDS['celebrate']
    if event in ('PostToolUseFailure', 'PermissionDenied') and use_id:
        jobs.pop(use_id[:100], None)  # the launch itself failed or was refused
    if event in ('Stop', 'StopFailure', 'Interrupt') or (event == 'PostToolUseFailure' and payload.get('is_interrupt')):
        end_turn(row, now)
    # Missed SubagentStop events must not pin a card on "delegating" forever.
    row['subagents'] = json.dumps({k: v for k, v in agents.items() if now - v < 7200})
    row['jobs'] = json.dumps({k: v for k, v in jobs.items() if now - v < JOB_TTL})
    row['state'] = state_for(event, payload, row.get('state') or 'idle')
    # Who is waiting on the user: '' for the main thread, else a subagent's ID. Only the waiter's own
    # progress clears its prompt, so a background subagent's tool calls can't hide the parent's (or another
    # subagent's) pending permission, and a later Stop or idle notice can't either.
    waiters = set(json.loads(row.get('waiters') or '[]'))
    who = sub[:80] if isinstance(sub, str) else ''
    if (event == 'Interrupt' or (event == 'PostToolUseFailure' and payload.get('is_interrupt'))
            or (event == 'SessionStart' and payload.get('source') != 'compact')):
        waiters.clear()
    elif event != 'Notification':
        waiters.discard(who)
    if state_for(event, payload) == 'waiting':  # this event itself asks, not a state carried over
        if event != 'Notification' or not waiters:  # a permission notice repeats a request we may know of
            waiters.add(who)
    if waiters:
        row['state'] = 'waiting'
    row['waiters'] = json.dumps(sorted(waiters))
    if row['state'] in BUSY and not row.get('turn_started'):
        row['turn_started'] = now  # a prompt, or Claude woken by a finished background job


def forget(db, sid):
    db.execute('DELETE FROM sessions WHERE id=?', (sid,))
    db.execute('INSERT OR REPLACE INTO ended VALUES (?,?)', (sid, time.time()))


_codex_meta = {}  # rollout path -> (thread id, cwd, thread source, started); a rollout's first line never changes
_codex_turns = {}  # rollout path -> (inode, bytes scanned, state, stamp)
_codex_scan = [0.0, {}]  # last scan time, rollout path -> owning (pid, start)


def codex_session_meta(path):
    """The rollout's opening session_meta: thread id, folder, and what started the thread."""
    if path not in _codex_meta:
        try:
            with open(path, 'rb') as f:
                d = json.loads(f.readline(1 << 20))
            p = d['payload']
            if d.get('type') != 'session_meta' or not isinstance(p.get('id'), str):
                return None
            _codex_meta[path] = (p['id'], p.get('cwd') if isinstance(p.get('cwd'), str) else '',
                                 p.get('thread_source'), iso_ts(p.get('timestamp')))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
    return _codex_meta[path]


def codex_thread_source(path):
    meta = codex_session_meta(path)
    return meta and meta[2]


def codex_open_rollouts(now):
    """Rollouts a running Codex process holds open, i.e. the threads it has loaded. Rescanned every 3 s."""
    if now - _codex_scan[0] < 3:
        return _codex_scan[1]
    found, root = {}, str(CODEX / 'sessions') + os.sep
    for proc in Path('/proc').iterdir():
        try:
            if not proc.name.isdigit() or not (proc / 'comm').read_text().startswith(CODEX_COMM):
                continue
            info = process_info(proc.name)
            for fd in (proc / 'fd').iterdir():
                target = os.readlink(fd)
                if info and target.startswith(root) and Path(target).name.startswith('rollout-') and target.endswith('.jsonl'):
                    found[target] = (int(proc.name), info['start'])
        except OSError:
            continue
    _codex_scan[:] = [now, found]
    return found


TURN_EVENT = re.compile(rb'"timestamp":\s*"([^"\n]+)"[^\n]{0,160}?"type":\s*"event_msg",\s*"payload":\s*\{\s*'
                        rb'"type":\s*"(task_started|task_complete|turn_aborted)"')
TURN_STATES = {b'task_started': 'thinking', b'task_complete': 'ready', b'turn_aborted': 'idle'}


def last_turn_event(f, start, end):
    f.seek(start)
    found = None
    for found in TURN_EVENT.finditer(f.read(end - start)):
        pass
    return found and (TURN_STATES[found.group(2)], iso_ts(found.group(1).decode()))


def codex_turn_state(path):
    """How the thread's last turn stands: running (thinking), finished (ready) or aborted (idle).
    A long turn can write megabytes of tool output after it starts, so the first look walks back
    through the whole rollout until it meets a turn event; later looks read only appended bytes."""
    try:
        st = os.stat(path)
        cached = _codex_turns.get(path)
        if cached and cached[0] == st.st_ino and cached[1] == st.st_size:
            return cached[2:]
        with open(path, 'rb') as f:
            if cached and cached[0] == st.st_ino and cached[1] < st.st_size:
                found = last_turn_event(f, max(0, cached[1] - 1024), st.st_size) or (cached[2:])
            else:
                found, end = None, st.st_size
                while end > 0 and not found:
                    begin = max(0, end - (4 << 20))
                    found = last_turn_event(f, begin, min(st.st_size, end + 1024))  # overlap: no event split
                    end = begin
    except OSError:
        return 'idle', time.time()
    state, stamp = found or ('idle', st.st_mtime)
    _codex_turns[path] = (st.st_ino, st.st_size, state, stamp or st.st_mtime)
    return _codex_turns[path][2:]


def record(payload, owner=None, provider='claude'):
    sid = payload.get('session_id')
    event = payload.get('hook_event_name')
    if not isinstance(sid, str) or not sid or len(sid) > 256 or event not in (
            CODEX_EVENTS if provider == 'codex' else EVENTS):
        return
    path = payload.get('transcript_path')
    # A Codex chat always has a rollout. Ephemeral sessions (an approval's Guardian review) report none and
    # may never send Stop, and internal threads say what they are in the rollout's first line.
    internal = provider == 'codex' and (not isinstance(path, str) or not path.endswith('.jsonl')
                                        or codex_thread_source(path) in CODEX_INTERNAL)
    with contextlib.closing(connect()) as db, db:
        if event == 'SessionEnd' or internal:
            forget(db, sid)
            return
        db.execute('DELETE FROM ended WHERE id=?', (sid,))
        old = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        if not old and provider == 'codex' and isinstance(path, str):
            twin = db.execute("SELECT * FROM sessions WHERE transcript=? AND source='metadata'", (path,)).fetchone()
            if twin:
                db.execute('DELETE FROM sessions WHERE id=?', (twin['id'],))
                old = dict(twin, id=sid)
        row = dict(old) if old else blank(sid)
        pid, start = owner if owner is not None else agent_parent(provider)
        if not pid and old:
            pid, start = old['pid'], old['proc_start']
        now = time.time()
        if not old:
            names = list(config()['avatars'])
            n = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
            row.update(avatar=names[n % len(names)], created=now, started=now)
        cwd = payload.get('cwd')
        if isinstance(cwd, str) and not row['cwd']:
            row['cwd'] = cwd  # the launch folder names the project; later cd's in the shell don't
        row['cwd'] = row['cwd'] or ''
        if isinstance(path, str) and path.endswith('.jsonl'):
            row['transcript'] = path
        mode = payload.get('permission_mode')
        if isinstance(mode, str):
            row['mode'] = mode[:30]
        apply(row, event, payload, now)
        row.update(pid=pid, proc_start=start, updated=now, source='hook', provider=provider)
        save(db, row)


def refresh_sessions():
    """Best-effort bootstrap from metadata, without reading any transcripts."""
    now = time.time()
    loaded = codex_open_rollouts(now)
    with contextlib.closing(connect()) as db, db:
        db.execute('DELETE FROM ended WHERE ended < ?', (now - 86400 * 7,))
        for row in db.execute('SELECT * FROM sessions').fetchall():
            codex, still_loaded = row['provider'] == 'codex', row['transcript'] in loaded
            if (row['pid'] and not alive(row['pid'], row['proc_start'])) or (
                    not row['pid'] and now - row['updated'] > 86400) or (
                    codex and not row['transcript']) or (
                    codex and row['source'] == 'metadata' and not still_loaded) or (
                    codex and not still_loaded and row['state'] in ('idle', 'ready', 'failed')
                    and now - row['updated'] > CODEX_IDLE):
                db.execute('DELETE FROM sessions WHERE id=?', (row['id'],))
            elif codex and not still_loaded and row['state'] in BUSY:
                # Not held open (e.g. the VS Code extension's chats): the rollout still shows a missed Stop.
                state, stamp = codex_turn_state(row['transcript'])
                if state != 'thinking' and stamp > row['updated']:
                    fixed = dict(row, state=state, updated=min(stamp, now))
                    end_turn(fixed, fixed['updated'])
                    save(db, fixed)
        names = list(config()['avatars'])
        for path in sorted((CLAUDE / 'sessions').glob('*.json')):
            try:
                m = json.loads(path.read_text())
                sid, pid, start = m['sessionId'], int(m['pid']), str(m['procStart'])
                if m.get('kind') != 'interactive' or not alive(pid, start):
                    continue
                if not isinstance(sid, str) or not isinstance(m.get('cwd', ''), str):
                    continue
                if db.execute('SELECT 1 FROM ended WHERE id=?', (sid,)).fetchone():
                    continue
                old = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
                state = {'busy': 'thinking', 'idle': 'idle', 'waiting': 'waiting'}.get(m.get('status'), 'idle')
                stamp = min(float(m.get('statusUpdatedAt', 0)) / 1000, now)
                started = min(float(m['startedAt']) / 1000, now) if m.get('startedAt') else None
                # Only a /rename name is a title; derived names just echo the project folder.
                label = m.get('name') if isinstance(m.get('name'), str) and m.get('nameSource') == 'user' else None
                row = dict(old) if old else blank(sid)
                if not old:
                    n = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
                    row.update(cwd=m.get('cwd', ''), pid=pid, proc_start=start, state=state, updated=stamp,
                               source='metadata', avatar=names[n % len(names)], created=now)
                elif old['source'] == 'metadata':
                    row.update(state=state, updated=stamp, pid=pid, proc_start=start)
                elif not old['pid']:
                    row.update(pid=pid, proc_start=start)
                elif old['state'] in BUSY and state == 'idle' and stamp > old['updated']:
                    # A terminal interrupt may produce no Stop hook. Newer local
                    # idle metadata can clear busy states, but never clears an alert.
                    row.update(state='idle', updated=stamp, source='metadata')
                    end_turn(row, stamp)
                row.update(started=started or row['started'] or row['created'], name=label,
                           cwd=m.get('cwd') or row['cwd'])
                if dict(old or {}) != row:
                    save(db, row)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for path, (pid, start) in loaded.items():
            meta = codex_session_meta(path)
            if not meta or meta[2] in CODEX_INTERNAL:
                continue
            sid, cwd, _, started = meta
            ended = db.execute('SELECT ended FROM ended WHERE id=?', (sid,)).fetchone()
            if ended:
                # Ended for good, unless a Codex process started since then has reopened the thread
                # (Codex sends no SessionStart when an app restart merely loads it again).
                reopened = started_at(start)
                if not reopened or ended[0] >= reopened:
                    continue
                db.execute('DELETE FROM ended WHERE id=?', (sid,))
            old = db.execute('SELECT * FROM sessions WHERE id=? OR transcript=?', (sid, path)).fetchone()
            state, stamp = codex_turn_state(path)
            stamp = min(stamp, now)
            row = dict(old) if old else blank(sid)
            if not old:
                n = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
                row.update(cwd=cwd, pid=pid, proc_start=start, state=state, updated=stamp, source='metadata',
                           provider='codex', transcript=path, avatar=names[n % len(names)], created=now,
                           started=min(started or now, now))
            elif old['source'] == 'metadata':
                row.update(state=state, updated=stamp, pid=pid, proc_start=start)
            elif old['state'] in BUSY and state != 'thinking' and stamp > old['updated']:
                row.update(state=state, updated=stamp, source='metadata')  # a missed Stop or Interrupt
                end_turn(row, stamp)
            if row['source'] == 'metadata':
                if state == 'thinking':
                    # Rollout task_started is also a turn clock when hooks are unavailable.
                    # Keep it stable across refreshes; a newer task starts a new clock.
                    if not row.get('turn_started') or (old and stamp > old['updated']):
                        row['turn_started'] = stamp
                else:
                    end_turn(row, stamp)
            if dict(old or {}) != row:
                save(db, row)
        return [dict(r) for r in db.execute('SELECT * FROM sessions ORDER BY created,id')]


class Usage:
    """Incremental reader of a transcript's numeric usage fields; message text is never kept."""
    CHUNK = 8 << 20  # bytes per update, so a huge transcript cannot freeze the window

    def __init__(self, path):
        self.path = Path(path)
        self.reset(None)

    def reset(self, inode):
        self.inode, self.offset, self.messages, self.stamps = inode, 0, {}, {}
        self.model, self.context, self.peak, self.compactions = None, 0, 0, 0
        self.finished = set()
        self.titles = {}  # 'ai-title' / 'custom-title' record -> latest title Claude Code gave the chat

    def update(self):
        try:
            st = self.path.stat()
            if st.st_ino != self.inode or st.st_size < self.offset:
                self.reset(st.st_ino)
            if st.st_size == self.offset:
                return self
            with open(self.path, 'rb') as f:
                f.seek(self.offset)
                chunk = f.read(self.CHUNK)
        except OSError:
            return self
        end = chunk.rfind(b'\n') + 1
        if not end and len(chunk) == self.CHUNK:  # a single line larger than one chunk
            end = len(chunk)
        for line in chunk[:end].splitlines():
            self.parse(line)
        self.offset += end
        return self

    def parse(self, line):
        if b'"compact_boundary"' in line:
            self.compactions += 1
        if b'<task-notification>' in line:
            self.finished.update(i.decode() for i, status in NOTICE.findall(line)
                                 if status not in (b'running', b'pending', b'progress'))
        if b'"type":"ai-title"' in line or b'"type":"custom-title"' in line:
            try:
                d = json.loads(line)
                kind = d['type']
                title = d[{'ai-title': 'aiTitle', 'custom-title': 'customTitle'}[kind]]
                if isinstance(title, str) and title.strip():
                    self.titles[kind] = ' '.join(title.split())[:200]
                    return
            except (ValueError, KeyError, TypeError):
                pass
        if b'"usage"' not in line or b'"assistant"' not in line:
            return
        try:
            d = json.loads(line)
            msg = d['message']
            u = msg['usage']
            counts = tuple(int(u.get(k) or 0) for k in USAGE_KEYS)
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        if d.get('type') != 'assistant':
            return
        key = msg.get('id') or d.get('uuid') or len(self.messages)
        self.messages[key] = counts
        if key not in self.stamps:
            self.stamps[key] = iso_ts(d.get('timestamp')) or time.time()
        if not d.get('isSidechain'):
            self.model = msg.get('model') or self.model
            self.context = sum(counts[:3])
            self.peak = max(self.peak, self.context)

    def totals(self):
        return [sum(c[i] for c in self.messages.values()) for i in range(len(USAGE_KEYS))]


class CodexUsage(Usage):
    """Same counters from a Codex rollout: per-response token records, model and window settings."""
    def reset(self, inode):
        super().reset(inode)
        self.window = None

    def parse(self, line):
        if line.startswith(b'{"timestamp"') and b'"type":"compacted"' in line[:120]:
            self.compactions += 1
            return
        if b'"token_usage_record"' not in line and b'"model' not in line:
            return
        try:
            d = json.loads(line)
            p = d['payload']
            kind = d['type']
            if kind == 'token_usage_record':
                u = p['usage']
                fresh, read, write, out = (int(u.get(k) or 0) for k in (
                    'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens'))
                # Codex counts cached tokens inside input_tokens; keep Claude's (fresh, write, read, out) order.
                self.messages[p.get('response_id') or len(self.messages)] = (max(0, fresh - read - write), write, read, out)
                self.context = fresh
                self.peak = max(self.peak, fresh)
            elif kind == 'turn_context':
                self.model = p.get('model') or self.model
            elif kind == 'event_msg' and p.get('type') in ('task_started', 'token_count'):
                window = p.get('model_context_window') or (p.get('info') or {}).get('model_context_window')
                self.window = int(window) if window else self.window
        except (ValueError, KeyError, TypeError, AttributeError):
            pass


_usage = {}  # Claude transcripts only: weighted_between calibrates Anthropic limits from these
_codex_usage = {}
_codex_titles = [None, {}]  # session_index.jsonl mtime, id -> thread name


def clean_title(text):
    """One line of plain text: markdown links keep their label, e.g. '[@Pets](plugin://...)' -> '@Pets'."""
    text = re.sub(r'\[([^\]\n]*)\]\([^)\s]*\)', r'\1', text)
    return ' '.join(text.split())[:200]


_codex_names = {}  # thread id -> (looked up at, title)


def codex_state_title(sid, now=None):
    """The chat name Codex shows: the thread's name (generated or renamed), else its title (first message).
    Read-only from Codex's state database, at most every 5 s per thread."""
    now = now or time.time()
    cached = _codex_names.get(sid)
    if cached and now - cached[0] < 5:
        return cached[1]
    title = None
    with contextlib.suppress(sqlite3.Error, OSError):
        uri = (CODEX / 'state_5.sqlite').as_uri() + '?mode=ro'
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as db:
            found = db.execute('SELECT name, title FROM threads WHERE id=?', (sid,)).fetchone()
        for value in found or ():
            if isinstance(value, str) and clean_title(value):
                title = clean_title(value)
                break
    _codex_names[sid] = (now, title)
    return title


def codex_title(sid):
    title = codex_state_title(sid) if (CODEX / 'state_5.sqlite').exists() else None
    if title:
        return title
    path = CODEX / 'session_index.jsonl'
    try:
        stamp = path.stat().st_mtime
        if stamp != _codex_titles[0]:
            names = {}
            for line in path.read_text().splitlines():
                with contextlib.suppress(ValueError, KeyError, TypeError, AttributeError):
                    d = json.loads(line)
                    if isinstance(d['thread_name'], str) and clean_title(d['thread_name']):
                        names[d['id']] = clean_title(d['thread_name'])
            _codex_titles[:] = [stamp, names]
    except OSError:
        return None
    return _codex_titles[1].get(sid)


def codex_logged_limits(now=None):
    """Codex's own last rate-limit reading from its newest rollouts: local files, no request, no token."""
    now = now or time.time()
    days = [CODEX / 'sessions' / time.strftime('%Y/%m/%d', time.localtime(now - 86400 * i)) for i in range(8)]
    files = sorted((f for d in days for f in d.glob('rollout-*.jsonl')), key=lambda f: f.stat().st_mtime, reverse=True)
    best = None
    for path in files[:4]:
        try:
            with open(path, 'rb') as f:
                f.seek(max(0, path.stat().st_size - (1 << 20)))
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if b'"rate_limits"' not in line or b'"limit_id":"codex"' not in line:
                continue
            with contextlib.suppress(ValueError, KeyError, TypeError, AttributeError):
                d = json.loads(line)
                stamp, limits = iso_ts(d['timestamp']), d['payload']['rate_limits']
                if stamp and (not best or stamp > best['fetched']):
                    best = {'fetched': stamp, 'source': 'log', 'plan': limits.get('plan_type'), 'windows': {},
                            'reached': bool(limits.get('rate_limit_reached_type'))}
                    for key in ('primary', 'secondary'):
                        w = limits.get(key)
                        if isinstance(w, dict) and isinstance(w.get('used_percent'), (int, float)):
                            best['windows'][key] = {'used': float(w['used_percent']), 'resets': w.get('resets_at'),
                                                    'minutes': w.get('window_minutes')}
                break
    return best


def codex_binary():
    """A Codex executable with an app server: config "codex_binary", the desktop app's, VS Code's, else PATH."""
    candidates = [config().get('codex_binary'), '/usr/lib/chatgpt/resources/codex']
    candidates += sorted(map(str, Path.home().glob('.vscode/extensions/openai.chatgpt-*/bin/*/codex')),
                         key=os.path.getmtime, reverse=True)
    candidates.append(shutil.which('codex'))
    return next((c for c in candidates if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


def fetch_codex_limits(binary, timeout=20):
    """account/rateLimits/read through Codex's own app server, as Codex's usage panel does. Codex makes the
    request with its own login (and refreshes it itself); the monitor never reads the OpenAI token."""
    import select
    messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                 'params': {'clientInfo': {'name': 'agent-monitor', 'title': 'Agent Monitor', 'version': '1.0'}}},
                {'jsonrpc': '2.0', 'method': 'initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'account/rateLimits/read'}]
    proc = subprocess.Popen([binary, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        proc.stdin.write(b''.join(json.dumps(m).encode() + b'\n' for m in messages))
        proc.stdin.flush()
        end, pending, fd = time.time() + timeout, b'', proc.stdout.fileno()
        while True:
            # Raw reads: a buffered readline could swallow a second reply that select() then never reports.
            while b'\n' not in pending:
                if not select.select([fd], [], [], max(0, end - time.time()))[0]:
                    raise TimeoutError('no reply from Codex app server')
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    raise RuntimeError('Codex app server exited')
                pending += chunk
            line, pending = pending.split(b'\n', 1)
            if not line.strip():
                continue
            reply = json.loads(line)
            if reply.get('id') in (1, 2) and 'error' in reply:
                raise RuntimeError(str(reply['error'].get('message') or reply['error'])[:120])
            if reply.get('id') == 2:
                return reply['result']
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()  # the app server exits on end of input
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def parse_codex_limits(result, fetched):
    limits = (result.get('rateLimitsByLimitId') or {}).get('codex') or result['rateLimits']
    windows = {}
    for key in ('primary', 'secondary'):
        w = limits.get(key)
        if isinstance(w, dict) and isinstance(w.get('usedPercent'), (int, float)):
            windows[key] = {'used': float(w['usedPercent']), 'resets': w.get('resetsAt'),
                            'minutes': w.get('windowDurationMins')}
    credits = (result.get('rateLimitResetCredits') or {}).get('availableCount')
    return {'fetched': fetched, 'source': 'api', 'plan': limits.get('planType'), 'windows': windows,
            'reached': bool(limits.get('rateLimitReachedType')),
            'reset_credits': credits if isinstance(credits, int) else None}


def load_codex_state():
    try:
        return json.loads((DATA / 'codex_limits.json').read_text())
    except (OSError, ValueError):
        return {}


def poll_codex_limits(now=None):
    """One refresh through Codex's app server; failures back off like the Anthropic poll."""
    now = now or time.time()
    state, binary = load_codex_state(), codex_binary()
    if not binary:
        state['error'] = 'Codex not found'
    elif now >= state.get('backoff_until', 0):
        try:
            state = dict(parse_codex_limits(fetch_codex_limits(binary), now), failures=0, error=None, backoff_until=0)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as e:
            state['error'] = f'Codex usage: {e}'[:120] if isinstance(e, RuntimeError) else f'Codex usage {type(e).__name__}'
            state['failures'] = state.get('failures', 0) + 1
            state['backoff_until'] = now + min(POLL_MAX, POLL_SECONDS * 2 ** state['failures'])
    state['checked'] = now
    atomic_json(DATA / 'codex_limits.json', state)
    return state


def codex_limits(now=None):
    """The newest Codex reading: a poll of the app server, or one Codex logged with a response since."""
    now = now or time.time()
    polled, logged = load_codex_state(), codex_logged_limits(now)
    best = logged if logged and logged['fetched'] > polled.get('fetched', 0) else polled if polled.get('windows') else logged
    if best is None:
        return {'error': polled['error']} if polled.get('error') else None
    # Reset credits only come from polls; they change rarely, so a newer logged reading keeps them.
    return dict(best, reset_credits=polled.get('reset_credits'), error=polled.get('error'))


def codex_view(reading, now):
    out = {}
    for key, w in ((reading or {}).get('windows') or {}).items():
        used, resets = w['used'], w['resets']
        if resets and now > resets:
            used, resets = 0.0, None
        out[key] = {'left': max(0.0, 100 - used), 'estimated': False, 'resets': resets}
    return out


def window_span(minutes, fallback):
    if not minutes:
        return fallback
    return f'{minutes // 1440}d' if minutes % 1440 == 0 else f'{minutes // 60}h' if minutes % 60 == 0 else f'{minutes}m'


def transcript_for(row):
    path = row.get('transcript')
    if path and Path(path).is_file():
        return Path(path)
    found = sorted((CLAUDE / 'projects').glob(f'*/{row["id"]}.jsonl')) if '/' not in row['id'] else []
    return found[0] if found else None


def context_window(model, peak):
    configured = config().get('context_window')
    if isinstance(configured, int) and configured > 0:
        return configured
    model = model or ''
    return 1_000_000 if '[1m]' in model or 'opus-5' in model or peak > 200_000 else 200_000


def iso_ts(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return None


def weighted_between(start, end):
    """Weighted tokens this machine spent in (start, end], across every transcript read so far."""
    total = 0.0
    for reader in list(_usage.values()):
        stamps = reader.stamps
        for key, counts in list(reader.messages.items()):
            if start < stamps.get(key, 0) <= end:
                total += sum(c * w for c, w in zip(counts, TOKEN_WEIGHTS))
    return total


def account():
    """Display fields of the signed-in Claude account; credentials are never read here."""
    try:
        a = json.loads(CLAUDE_JSON.read_text()).get('oauthAccount') or {}
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(a, dict) or not a.get('emailAddress'):
        return None
    tier = re.search(r'max_(\d+x)', a.get('userRateLimitTier') or a.get('organizationRateLimitTier') or '')
    return {'email': a['emailAddress'], 'org': a.get('organizationName') or '',
            'plan': PLANS.get(a.get('organizationType'), a.get('organizationType') or '?'),
            'tier': f'Max {tier.group(1)}' if tier else None, 'role': a.get('organizationRole')}


def read_token():
    """Claude Code's current access token, or None. Never refreshed here: that would rotate Claude's login."""
    try:
        o = json.loads((CLAUDE / '.credentials.json').read_text())['claudeAiOauth']
        if float(o.get('expiresAt', 0)) / 1000 > time.time() + 60 and isinstance(o['accessToken'], str):
            return o['accessToken']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def fetch_usage(token):
    req = urllib.request.Request(USAGE_URL, headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20',
        'Accept': 'application/json', 'User-Agent': 'agent-monitor/1.0'})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def parse_limits(d, fetched, source):
    windows = {}
    for key, _ in WINDOWS:
        w = d.get(key)
        if isinstance(w, dict) and isinstance(w.get('utilization'), (int, float)):
            windows[key] = {'used': float(w['utilization']), 'resets': iso_ts(w.get('resets_at'))}
    extra = [{'kind': str(x.get('kind')), 'percent': x.get('percent'), 'resets': iso_ts(x.get('resets_at'))}
             for x in d.get('limits') or [] if isinstance(x, dict) and x.get('kind') not in ('session', 'weekly_all')]
    return {'fetched': fetched, 'source': source, 'windows': windows, 'extra': extra}


def cached_limits():
    """Claude Code's own last /usage reading, if it belongs to the signed-in account."""
    try:
        d = json.loads(CLAUDE_JSON.read_text())
        c = d['cachedUsageUtilization']
        if c.get('accountUuid') != (d.get('oauthAccount') or {}).get('accountUuid'):
            return None
        return parse_limits(c['utilization'], float(c['fetchedAtMs']) / 1000, 'cache')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def load_limits():
    try:
        return json.loads((DATA / 'limits.json').read_text())
    except (OSError, ValueError):
        return {}


def calibrate(state, reading):
    """Learn percent-per-weighted-token from two readings in the same window, at least 2 points apart."""
    anchors, rates = state.setdefault('anchors', {}), state.setdefault('rates', {})
    for key, w in reading['windows'].items():
        a = anchors.get(key)
        same = a and w['resets'] and a.get('resets') and abs(w['resets'] - a['resets']) < 600
        if not same or w['used'] < a['used']:
            anchors[key] = {'fetched': reading['fetched'], 'used': w['used'], 'resets': w['resets']}
        elif w['used'] - a['used'] >= 2:
            spent = weighted_between(a['fetched'], reading['fetched'])
            if spent > 0:
                rates[key] = (w['used'] - a['used']) / spent
            anchors[key] = {'fetched': reading['fetched'], 'used': w['used'], 'resets': w['resets']}


def poll_limits(now=None):
    """One refresh: the usage API with Claude Code's current token, else Claude Code's own cache."""
    now = now or time.time()
    state, who = load_limits(), account()
    email = who and who['email']
    if state.get('account') != email:
        state = {'account': email}  # another login: old readings and calibration do not apply
    reading, token = None, read_token()
    if not token:
        state['error'] = 'no valid Claude login token'
    elif now >= state.get('backoff_until', 0):
        retry = None
        try:
            reading = parse_limits(fetch_usage(token), now, 'api')
            state.update(failures=0, error=None, backoff_until=0)
        except urllib.error.HTTPError as e:
            state['error'] = f'usage API HTTP {e.code}'
            with contextlib.suppress(TypeError, ValueError, AttributeError):
                retry = float(e.headers.get('retry-after'))
        except (OSError, ValueError) as e:
            state['error'] = f'usage API {type(e).__name__}'
        if not reading:
            state['failures'] = state.get('failures', 0) + 1
            delay = min(POLL_MAX, POLL_SECONDS * 2 ** state['failures'])
            state['backoff_until'] = now + max(delay, min(retry or 0, 6 * 3600))
    cached = cached_limits()
    if not reading and cached and cached['fetched'] > state.get('fetched', 0):
        reading = cached
    if reading:
        calibrate(state, reading)
        state.update(reading)
    state['checked'] = now
    atomic_json(DATA / 'limits.json', state)
    return state


def limit_view(state, now):
    """Remaining percent per window: last reading plus calibrated local spend since it."""
    out = {}
    for key, w in (state.get('windows') or {}).items():
        used, resets, since = w['used'], w['resets'], state.get('fetched', now)
        if resets and now > resets:
            used, since, resets = 0.0, resets, None  # the window rolled over after the reading
        rate = (state.get('rates') or {}).get(key)
        extra = rate * weighted_between(since, now) if rate else 0.0
        out[key] = {'left': max(0.0, 100 - used - extra), 'estimated': extra >= 0.5, 'resets': resets}
    return out


def limit_text(view, state, now):
    if view is None:
        return state.get('error') or 'waiting for first reading'
    text = f'{"~" if view["estimated"] else ""}{view["left"]:.0f}% left · '
    text += f'resets {duration(view["resets"] - now)}' if view['resets'] else 'not started'
    if now - state.get('fetched', now) > 900:
        text += f' · {duration(now - state["fetched"])} old'
    return text


def session_usage(row):
    path = transcript_for(row)
    if not path:
        return None
    if row.get('provider') == 'codex':
        r = _codex_usage.setdefault(str(path), CodexUsage(path)).update()
        if not r.messages and not r.model:
            return None  # the rollout has no turn yet
        fresh, write, read, out = r.totals()
        return {'model': r.model, 'context': r.context, 'window': r.window or context_window(r.model, r.peak),
                'input': fresh + write + read, 'cache_read': read, 'cache_write': write, 'output': out,
                'requests': len(r.messages), 'compactions': r.compactions, 'finished': set(),
                'title': codex_title(row['id']), 'loading': r.path.exists() and r.offset < r.path.stat().st_size}
    readers = [_usage.setdefault(str(path), Usage(path)).update()]
    for sub in sorted(path.with_suffix('').glob('subagents/*.jsonl')):
        readers.append(_usage.setdefault(str(sub), Usage(sub)).update())
    main = readers[0]
    fresh, write, read, out = [sum(t) for t in zip(*(r.totals() for r in readers))]
    return {'model': main.model, 'context': main.context, 'window': context_window(main.model, main.peak),
            'input': fresh + write + read, 'cache_read': read, 'cache_write': write, 'output': out,
            'requests': sum(len(r.messages) for r in readers), 'compactions': main.compactions,
            'finished': main.finished,
            'title': main.titles.get('custom-title') or main.titles.get('ai-title'),
            'loading': any(r.offset < r.path.stat().st_size for r in readers if r.path.exists())}


def reconcile(row, usage):
    """Derive live background work; a stopped session with pending jobs shows as background."""
    finished = usage['finished'] if usage else set()
    row['pending_jobs'] = sum(1 for k in json.loads(row.get('jobs') or '{}') if k not in finished)
    row['pending_agents'] = len(json.loads(row.get('subagents') or '{}'))
    if row['state'] in ('ready', 'idle') and (row['pending_jobs'] or row['pending_agents']):
        row['state'] = 'background'
    return row


def duration(seconds):
    s = int(max(0, seconds or 0))
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m {s % 60:02d}s' if s < 600 else f'{s // 60}m'
    if s < 86400:
        return f'{s // 3600}h {s % 3600 // 60:02d}m'
    return f'{s // 86400}d {s % 86400 // 3600}h'


def tokens(n):
    n = n or 0
    for size, unit in ((1e6, 'M'), (1e3, 'k')):
        if n >= size:
            return f'{n / size:.1f}'.rstrip('0').rstrip('.') + unit
    return str(int(n))


def animation(row, now):
    if row.get('flash') and (row.get('flash_until') or 0) > now:
        if not (row['flash'] == 'celebrate' and row['state'] == 'background'):
            return row['flash']
    if row['state'] == 'ready' and now - (row['updated'] or now) > READY_WAVE:
        return 'idle'
    return row['state'] if row['state'] in LABELS else 'idle'


def status_text(row):
    text = LABELS.get(row['state'], row['state'])
    if row['state'] in ('reading', 'working') and row.get('tool'):
        text += ' · ' + row['tool'].removeprefix('mcp__')[:18]
    agents = len(json.loads(row.get('subagents') or '{}'))
    if agents and row['state'] == 'delegating':
        text += f' ×{agents}'
    if row['state'] == 'background':
        text += f' ×{(row.get("pending_jobs") or 0) + (row.get("pending_agents") or 0)}'
    return text + (' · detected' if row['source'] == 'metadata' else '')


ICON_BORDER = '#0b0f18'
ICON_CUT = (0, 0, 0, 0)  # hard-set alpha 0, carving a transparent hole rather than blending one


def draw_icon(state, size=18):
    """One small original pixel-art glyph per state, in the idiom of classic RPG status icons
    (sword = acting, skull = KO'd, sleeping moon = idle, question mark = needs a choice, ...) —
    hand-drawn shapes on a tiny canvas, not art traced or copied from any specific game.
    Pure PIL; safe to call without a Tk root, so this is unit-testable headlessly.
    """
    from PIL import Image, ImageDraw
    fg = COLORS.get(state, '#a6b0c3')
    im = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if state == 'idle':  # sleep: a crescent moon
        d.ellipse((5, 5, 27, 27), fill=fg, outline=ICON_BORDER, width=2)
        d.ellipse((12, 2, 33, 23), fill=ICON_CUT)
    elif state == 'thinking':  # processing: ascending ellipsis
        for cx, cy, r in ((8, 23, 3), (16, 18, 3.5), (24, 12, 4)):
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fg, outline=ICON_BORDER, width=1)
    elif state == 'reading':  # an open book: two bold wedge-pages, a dark spine between them
        d.polygon([(16, 7), (4, 13), (16, 25)], fill=fg, outline=ICON_BORDER)
        d.polygon([(16, 7), (28, 13), (16, 25)], fill=fg, outline=ICON_BORDER)
        d.line((16, 6, 16, 26), fill=ICON_BORDER, width=2)
    elif state == 'working':  # acting: a sword
        d.polygon([(16, 2), (19, 7), (19, 21), (13, 21), (13, 7)], fill=fg, outline=ICON_BORDER)
        d.rectangle((9, 21, 23, 24), fill=fg, outline=ICON_BORDER)
        d.rectangle((14, 24, 18, 30), fill=fg, outline=ICON_BORDER)
    elif state == 'delegating':  # passed off to someone else: a double chevron
        d.polygon([(5, 8), (15, 16), (5, 24)], fill=fg, outline=ICON_BORDER)
        d.polygon([(16, 8), (26, 16), (16, 24)], fill=fg, outline=ICON_BORDER)
    elif state == 'compacting':  # packing things away: a chest, dark body with a bright lid+lock
        d.rectangle((6, 16, 26, 28), fill=ICON_BORDER, outline=fg, width=1)
        d.rounded_rectangle((6, 9, 26, 17), radius=4, fill=fg, outline=ICON_BORDER, width=1)
        d.ellipse((14, 19, 18, 23), fill=fg)
    elif state == 'waiting':  # a question for you: "?"
        d.arc((9, 3, 24, 19), 200, 380, fill=fg, width=4)
        d.line((17, 17, 17, 22), fill=fg, width=4)
        d.ellipse((14, 25, 20, 31), fill=fg)
    elif state == 'background':  # still running, out of sight: an hourglass, solid caps, hollow glass
        d.rectangle((6, 3, 26, 8), fill=fg, outline=ICON_BORDER)
        d.rectangle((6, 24, 26, 29), fill=fg, outline=ICON_BORDER)
        d.polygon([(8, 8), (24, 8), (16, 16)], outline=fg, width=2)
        d.polygon([(8, 24), (24, 24), (16, 16)], outline=fg, width=2)
        d.ellipse((14, 14, 18, 18), fill=fg)  # the falling sand, caught mid-fall
    elif state == 'ready':  # a victory sparkle
        d.line((16, 1, 16, 31), fill=fg, width=2)
        d.line((1, 16, 31, 16), fill=fg, width=2)
        d.polygon([(16, 6), (21, 16), (16, 26), (11, 16)], fill=fg, outline=ICON_BORDER)
    elif state == 'failed':  # KO: a skull, with large bold eye sockets, the single boldest shape here
        d.ellipse((5, 3, 27, 21), fill=fg, outline=ICON_BORDER, width=2)
        d.rectangle((11, 18, 21, 26), fill=fg, outline=ICON_BORDER)
        d.ellipse((8, 8, 15, 16), fill=ICON_BORDER)
        d.ellipse((17, 8, 24, 16), fill=ICON_BORDER)
        d.rectangle((13, 21, 15, 24), fill=ICON_BORDER)
        d.rectangle((17, 21, 19, 24), fill=ICON_BORDER)
    else:
        d.ellipse((9, 9, 23, 23), fill=fg, outline=ICON_BORDER, width=2)
    im = im.resize((16, 16), Image.Resampling.NEAREST)  # a deliberately low-res pass first
    return im.resize((size, size), Image.Resampling.NEAREST)  # ...then blocky pixels, not smooth art


def draw_ui_icon(name, size=18):
    """Header glyphs drawn like draw_icon: 'help' (?), 'details' (three lines), 'compact' (one),
    'sound' / 'muted' (a speaker with waves or a cross), the usage-bar markers 'clock' (the 5h window)
    and 'calendar' (the weekly one), and the card marks 'long' / 'too long' (a scroll, yellow or red: the
    context has grown), 'cold' (a snowflake: the prompt cache expired) and 'struggling' (a burst: a turn
    piling up tool errors or running very long)."""
    from PIL import Image, ImageDraw
    light, dark = '#d6deef', '#101830'
    im = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if name == 'help':
        d.ellipse((2, 2, 30, 30), fill=light, outline=ICON_BORDER, width=2)
        d.arc((10, 6, 22, 18), 200, 380, fill=dark, width=4)
        d.line((16, 16, 16, 20), fill=dark, width=4)
        d.rectangle((14, 23, 18, 27), fill=dark)
    elif name == 'details':
        for y in (6, 14, 22):
            d.rectangle((4, y, 28, y + 4), fill=light, outline=ICON_BORDER)
    elif name in ('sound', 'muted'):  # a speaker, with sound waves or a cross
        d.polygon([(3, 12), (9, 12), (17, 4), (17, 28), (9, 20), (3, 20)], fill=light, outline=ICON_BORDER)
        if name == 'sound':
            d.arc((12, 9, 24, 23), -50, 50, fill=light, width=3)
            d.arc((12, 3, 30, 29), -50, 50, fill=light, width=3)
        else:
            d.line((21, 10, 30, 22), fill='#ff919b', width=4)
            d.line((30, 10, 21, 22), fill='#ff919b', width=4)
    elif name == 'clock':  # the short window: a pocket watch, bright face, dark hands at ~5 o'clock
        d.rectangle((13, 0, 19, 4), fill='#ffd98a', outline=ICON_BORDER)
        d.ellipse((3, 4, 29, 30), fill='#ffd98a', outline=ICON_BORDER, width=2)
        d.ellipse((7, 8, 25, 26), fill=light)
        d.line((16, 17, 16, 10), fill=dark, width=3)
        d.line((16, 17, 21, 22), fill=dark, width=3)
    elif name == 'calendar':  # the weekly window: a page with red binding, rings and a grid of days
        d.rectangle((3, 6, 29, 29), fill=light, outline=ICON_BORDER, width=2)
        d.rectangle((3, 6, 29, 13), fill='#ff919b', outline=ICON_BORDER, width=2)
        for x in (8, 21):
            d.rectangle((x, 1, x + 3, 9), fill='#a6b0c3', outline=ICON_BORDER)
        for y in (16, 22):
            for x in (8, 14, 20):
                d.rectangle((x, y, x + 3, y + 3), fill=dark)
    elif name in ('long', 'too long'):  # a scroll: rolled ends, a parchment body with lines of text
        body = HEALTH_COLORS[name]
        d.rectangle((7, 6, 25, 26), fill=body, outline=ICON_BORDER, width=2)
        for y in (4, 24):
            d.rounded_rectangle((3, y, 29, y + 5), radius=2, fill='#c9a46a', outline=ICON_BORDER, width=2)
        for y in (12, 16, 20):
            d.line((11, y, 21, y), fill=dark, width=2)
    elif name == 'cold':  # a snowflake: three crossing bars with little branches
        ice = '#9fd8ff'
        for a, b in (((16, 2), (16, 30)), ((4, 9), (28, 23)), ((4, 23), (28, 9))):
            d.line((*a, *b), fill=ICON_BORDER, width=6)
        for a, b in (((16, 2), (16, 30)), ((4, 9), (28, 23)), ((4, 23), (28, 9))):
            d.line((*a, *b), fill=ice, width=3)
        d.ellipse((12, 12, 20, 20), fill=ice, outline=ICON_BORDER)
    elif name == 'struggling':  # a burst with a "!": trouble in this turn
        star = [(16 + r * math.cos(math.pi * i / 8 - math.pi / 2), 16 + r * math.sin(math.pi * i / 8 - math.pi / 2))
                for i, r in zip(range(16), [15, 10] * 8)]
        d.polygon(star, fill='#ffb070', outline=ICON_BORDER)
        d.rectangle((14, 6, 17, 17), fill=dark)
        d.rectangle((14, 20, 17, 23), fill=dark)
    else:  # compact
        d.rectangle((4, 14, 28, 18), fill=light, outline=ICON_BORDER)
    im = im.resize((16, 16), Image.Resampling.NEAREST)
    return im.resize((size, size), Image.Resampling.NEAREST)


HELP_STATES = {'idle': 'nothing running', 'thinking': 'between steps', 'reading': 'reading files or the web',
               'working': 'editing, running commands', 'delegating': 'handed to a subagent',
               'compacting': 'summarising its context', 'waiting': 'waiting on your answer',
               'background': 'stopped, jobs still running', 'ready': 'finished, your turn',
               'failed': 'the turn failed (API error)'}
HELP_SECTIONS = (
    ('WHAT THIS IS', 'One card for each open Claude Code or Codex chat on this computer. The avatar acts '
                     'out what that chat is doing right now. The tag in a card\'s corner says which.'),
    ('STATUS', None),  # filled with the icon legend
    ('USAGE BARS', 'One pair per provider. The clock bar is how much of the 5-hour usage window is left, '
                   'the calendar bar the weekly one, and when each resets. "pace" is how much of the window '
                   'you will have used by then if you carry on as you are: your last half hour for the 5-hour '
                   'window, your average so far for the week. A ~ means the Anthropic number is estimated '
                   'since the last check. The OpenAI line also says when the limit is reached and how many '
                   'free resets you have.'),
    ('SESSION HEALTH', 'marks'),  # filled with the mark legend; hover a mark on a card for its numbers
    ('CARD DETAILS', 'Each card shows the chat\'s title, its project and a short session ID. Details add '
                     'session age, active time, context used, tokens in and out, and the model. '
                     'The lines button at the top switches between full details and a compact view.'),
    ('PRIVACY', 'Everything comes from Claude Code\'s and Codex\'s own event hooks and session files '
                'on this computer. The monitor never sends messages to either and never spends tokens; '
                'the usage bars check every few minutes: one small request to Anthropic with your Claude '
                'login, and one to OpenAI that Codex makes itself (the monitor never reads that login).'),
    ('SOUND', 'A character with music plays it while its chat works, one chat at a time. The pace sets '
              'the mood: calm field music while you are on course to use under 80% of your allowance, '
              'battle music when you are on course to use nearly all of it, and boss music when you will '
              'run out before it resets or have under 10% left. A long task ends with a victory fanfare. '
              'Tracks shuffle and pick up where they left off. A chime marks a chat that needs your '
              'answer or failed, at most once a minute each. The speaker on a card silences that chat; '
              'the one at the top silences everything.'),
    ('TIPS', 'Click the name under an avatar to pick a different character for that chat. '
             'Drag the title bar to move the window.'),
)


def paint_window(canvas, w, h, cut=6):
    """Classic RPG dialogue box behind whatever is already on the canvas: a vertical blue
    gradient with chamfered corners and a dark / silver / steel bevel. Tagged 'bg'."""
    top, bottom = (0x2c, 0x4c, 0x9c), (0x0c, 0x14, 0x40)
    for y in range(h):
        t = y / max(1, h - 1)
        color = '#%02x%02x%02x' % tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
        inset = max(0, cut - y, y - (h - 1 - cut))
        canvas.create_line(inset, y, w - inset, y, fill=color, tags='bg')
    def bevel(o, color, width):
        k = max(1, cut - o)
        r, b = w - 1 - o, h - 1 - o
        canvas.create_polygon(o + k, o, r - k, o, r, o + k, r, b - k, r - k, b, o + k, b, o, b - k, o, o + k,
                              outline=color, fill='', width=width, tags='bg')
    bevel(1, '#0b0f18', 2)
    bevel(3, '#d6deef', 2)
    bevel(5, '#5a6f9c', 1)
    canvas.tag_lower('bg')


def details(row, usage, now):
    """Plain-text lines for the verbose card; also used by the status command."""
    turn = (now - row['turn_started']) if row.get('turn_started') else 0
    lines = [f'session {duration(now - (row.get("started") or row["created"]))}'
             f' · active {duration((row.get("busy") or 0) + turn)}']
    if turn:
        lines.append(f'this turn {duration(turn)}')
    elif row.get('last_turn') is not None:
        lines.append(f'last turn {duration(row["last_turn"])}')
    else:
        fresh = row['source'] == 'hook' and row['state'] not in BUSY
        lines.append('no turns yet' if fresh else 'turn timing from next event')
    counts = f'{row.get("prompts") or 0} prompts · {row.get("tools") or 0} tools'
    if row.get('tool_errors'):
        counts += f' · {row["tool_errors"]} err'
    lines.append(counts)
    jobs, agents = row.get('pending_jobs') or 0, len(json.loads(row.get('subagents') or '{}'))
    if jobs or agents:
        parts = [f'{n} {word}' + ('s' if n > 1 else '') for n, word in ((jobs, 'job'), (agents, 'agent')) if n]
        lines.append('background ' + ' · '.join(parts))
    if usage:
        pct = 100 * usage['context'] / usage['window'] if usage['window'] else 0
        lines.append(f'context {tokens(usage["context"])}/{tokens(usage["window"])} · {pct:.0f}%')
        cached = 100 * usage['cache_read'] / usage['input'] if usage['input'] else 0
        lines.append(f'in {tokens(usage["input"])} ({cached:.0f}% cached) · out {tokens(usage["output"])}')
        tail = [(usage['model'] or '?').removeprefix('claude-'), f'{usage["requests"]} calls']
        if usage['compactions']:
            tail.append(f'{usage["compactions"]} compact')
        lines.append(' · '.join(tail) + (' …' if usage['loading'] else ''))
    else:
        lines.append('context: waiting for transcript')
    meta = [row['mode']] if row.get('mode') and row['mode'] != 'default' else []
    lines.append(' · '.join(meta + [f'last event {duration(now - (row["updated"] or now))} ago']))
    return lines


HEALTH_COLORS = {'fresh': '#84e0bb', 'long': '#ffcf70', 'too long': '#ff919b'}
HELP_MARKS = (
    ('long', 'Context getting long (40% full, 150k tokens, or one compaction): /compact at a natural break, '
             'or start a new chat for the next task.'),
    ('too long', 'Too long (70% full, 300k tokens, or two compactions): answers suffer. Start a fresh chat; '
                 'ask the old one for a handoff summary first.'),
    ('cold', 'Cache gone cold: idle over 5 minutes, so the next message re-reads the whole context at full '
             'price. Batch follow-ups while it is warm.'),
    ('struggling', 'Trouble this turn: several tool errors, or a very long run of tool calls. Check whether '
                   'it is stuck.'))
HEALTH_ADVICE = {'long': '/compact at a natural break, or a new chat for the next task',
                 'too long': 'start a fresh chat; ask for a handoff summary first'}


def session_health(usage, cfg):
    """How far a chat's context has grown: ('fresh' | 'long' | 'too long', why), or (None, '') without
    transcript usage. Answers get worse as the context fills, and each compaction swaps detail for a summary.
    The token limits apply on 1M windows too, where 30% is already a very large context."""
    if not usage or not usage.get('window'):
        return None, ''
    context, compactions = usage.get('context') or 0, usage.get('compactions') or 0
    pct = 100 * context / usage['window']
    why = f'context {tokens(context)} · {pct:.0f}%' + (f' · {compactions} compact' if compactions else '')
    if pct >= setting(cfg, 'context_bad') or context >= setting(cfg, 'tokens_bad') or compactions >= 2:
        return 'too long', why
    if pct >= setting(cfg, 'context_warn') or context >= setting(cfg, 'tokens_warn') or compactions >= 1:
        return 'long', why
    return 'fresh', why


def session_flags(row, usage, now, cfg):
    """Best-practice warnings for one chat: a turn that looks stuck (many tool errors, or very long with
    many tool calls), and an idle chat whose prompt cache has expired, so its next turn re-reads the whole
    context at full price."""
    flags = []
    if row['state'] in BUSY | {'waiting'} and row.get('turn_started'):
        errors, calls, turn = row.get('turn_errors') or 0, row.get('turn_tools') or 0, now - row['turn_started']
        if errors >= setting(cfg, 'struggle_errors'):
            flags.append(f'{errors} tool errors this turn: check on it')
        elif turn >= setting(cfg, 'long_turn') and calls >= setting(cfg, 'long_turn_tools'):
            flags.append(f'long turn: {duration(turn)}, {calls} tools')
    context = (usage or {}).get('context') or 0
    if (row['state'] not in BUSY and context >= setting(cfg, 'cold_tokens')
            and now - (row.get('updated') or now) >= setting(cfg, 'cache_ttl')):
        flags.append(f'cache cold: next turn re-reads {tokens(context)}')
    return flags


def hook_command():
    return f'{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} hook'


def merge_hooks(settings, remove=False):
    result = json.loads(json.dumps(settings))
    hooks = result.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('Existing hooks setting must be an object; no changes made')
    cmd = hook_command()
    for event in EVENTS:
        groups = []
        for group in hooks.get(event, []):
            kept = [h for h in group.get('hooks', []) if h.get('command') != cmd]
            if kept:
                updated = dict(group)
                updated['hooks'] = kept
                groups.append(updated)
        if not remove:
            groups.append({'hooks': [{'type': 'command', 'command': cmd, 'timeout': 3}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        result.pop('hooks', None)
    return result


def install_hooks(remove=False):
    path = CLAUDE / 'settings.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    # The lock serializes this installer's own invocations, not other editors.
    with open(path.parent / '.agent-monitor-install.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = path.read_bytes() if path.exists() else b'{}'
        settings = json.loads(before)
        after = merge_hooks(settings, remove)
        if after == settings:
            print('Hooks already configured.')
            return
        if path.exists():
            backup = path.with_name(f'settings.agent-monitor-backup-{time.time_ns()}.json')
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
        if path.exists() and path.read_bytes() != before:
            raise RuntimeError('Settings changed during install; retry to merge current contents')
        atomic_json(path, after)
    print('Monitor hooks removed.' if remove else 'Monitor hooks installed; unrelated settings preserved.')


def codex_hook_command():
    return hook_command() + ' codex'


def merge_codex_hooks(current, remove=False):
    result = json.loads(json.dumps(current))
    hooks = result.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('Existing Codex hooks must be an object; no changes made')
    cmd = codex_hook_command()
    for event in CODEX_EVENTS:
        groups = []
        for group in hooks.get(event, []):
            kept = [h for h in group.get('hooks', []) if h.get('command') != cmd]
            if kept:
                groups.append(dict(group, hooks=kept))
        if not remove:
            # SessionEnd and Interrupt allow at most 3 seconds.
            groups.append({'hooks': [{'type': 'command', 'command': cmd, 'timeout': 3}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        result.pop('hooks', None)
    return result


def install_codex_hooks(remove=False):
    path = CODEX / 'hooks.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / '.agent-monitor-install.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = path.read_bytes() if path.exists() else b'{}'
        current = json.loads(before)
        after = merge_codex_hooks(current, remove)
        if after == current:
            print('Codex hooks already configured.')
            return
        if path.exists():
            backup = path.with_name(f'hooks.agent-monitor-backup-{time.time_ns()}.json')
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
        if path.exists() and path.read_bytes() != before:
            raise RuntimeError('hooks.json changed during install; retry to merge current contents')
        if after:
            atomic_json(path, after)
        else:
            path.unlink(missing_ok=True)
    print('Codex hooks removed.' if remove else
          'Codex hooks installed. Approve them once in Codex (/hooks, or the "Hooks need review" prompt).')


def ui_status():
    try:
        info = json.loads((DATA / 'ui.json').read_text())
        if alive(info['pid'], info['proc_start']):
            return info
    except (OSError, ValueError, KeyError):
        pass
    return None


def launch(wait=False):
    if ui_status() or not config().get('enabled', True) or config().get('headless'):
        return
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(DATA / 'monitor.log', 'a') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run'],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)
    if wait:
        for _ in range(30):
            if ui_status():
                return
            time.sleep(.1)
        raise RuntimeError(f'Monitor did not open; inspect {DATA / "monitor.log"}')


def gif_for(directory, anim):
    # Character-specific scenes override generic motion without changing hook states.
    profile = Path(directory) / 'animations.json'
    choices = ANIMATIONS[anim]
    if profile.exists():
        mapping = json.loads(profile.read_text())
        choices = mapping.get(anim, choices)
        if not isinstance(choices, (list, tuple)) or not choices or any(
                not isinstance(n, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', n) for n in choices):
            raise ValueError(f'Invalid animation choices for {anim} in {profile}')
    for name in choices:
        path = Path(directory) / (name + '.gif')
        if path.exists():
            return path
    return Path(directory) / 'idle.gif'


def variant_paths(directory, anim):
    """Read optional local scenes, accepting only plain GIF basenames."""
    paths = []
    manifest = Path(directory) / 'variants.json'
    if manifest.exists():
        names = json.loads(manifest.read_text()).get(anim, [])
        if not isinstance(names, list) or any(not isinstance(n, str) or
                not re.fullmatch(r'[a-z][a-z0-9-]*', n) for n in names):
            raise ValueError(f'Invalid {anim} scene list in {manifest}')
        for name in names:
            path = Path(directory) / (name + '.gif')
            if path.exists() and path not in paths:
                paths.append(path)
    return paths


def scene_paths(directory, anim):
    """Rotate execution and quiet background scenes; keep alerts and idle predictable."""
    paths = [gif_for(directory, anim)]
    if anim in SCENE_INTERVALS:
        paths.extend(p for p in variant_paths(directory, anim) if p not in paths)
    return paths


def choose_scene(card, avatar, anim, choices, now, rng=random):
    if anim not in SCENE_INTERVALS:
        return avatar, anim, choices[0]
    previous = card.get('encounter')
    if (card.get('encounter_avatar') != avatar or card.get('encounter_anim') != anim
            or previous not in choices or now >= card.get('encounter_until', 0)):
        options = [c for c in choices if c != previous] or choices
        card['encounter'] = rng.choice(options)
        card['encounter_avatar'] = avatar
        card['encounter_anim'] = anim
        card['encounter_until'] = now + rng.uniform(*SCENE_INTERVALS[anim])
    return avatar, anim, card['encounter']


def choose_transition(card, avatar, anim, key, shift, now):
    """Show an optional one-shot shift between busy roles, at most once per 30 seconds."""
    before = card.get('activity')
    card['activity'] = (avatar, anim)
    if anim not in BUSY or (before and before[0] != avatar):
        card.pop('restore_key', None)
        return key
    if card.get('restore_key'):
        card['restore_key'] = key
        return card['key']
    if (shift and before and before[1] in BUSY and before[1] != anim
            and now >= card.get('shift_after', 0)):
        card['restore_key'] = key
        card['shift_after'] = now + 30
        return shift
    return key


def card_palette(row, moods, cfg):
    provider = 'openai' if row.get('provider') == 'codex' else 'anthropic'
    mood = moods.get(provider, 'field')
    background = CARD_BACKDROPS.get(mood, CARD_BACKDROPS['field']) if cfg.get('mood_backdrops', True) else '#202a3d'
    return background, COLORS.get(row['state'], COLORS['idle'])


def fit_sprite(frame, size=(168, 126)):
    """Uniform 4:3 transparent stage. Preserve source proportions, never stretch."""
    from PIL import Image
    frame = frame.convert('RGBA')
    frame.thumbnail(size, Image.Resampling.NEAREST)
    stage = Image.new('RGBA', size)
    stage.paste(frame, ((size[0]-frame.width)//2, (size[1]-frame.height)//2))
    return stage


THEME_EXTS = ('.ogg', '.opus', '.mp3', '.flac', '.m4a', '.wav')
BACKGROUND_ROTATE = 120  # seconds each background-only chat's music plays before the next one's turn
THEME_GRACE = 3  # seconds a theme keeps playing through a pause, so back-to-back turns don't restart it
# Original jingles in a 16-bit RPG style, not tunes from any game. Each note: (start s, Hz, ring s, wave,
# gain, decay s); 'p25'/'p12' are pulse waves (melody), 'tri' a triangle (bass). Notes ring into each other
# like a harp, held ones get a little vibrato, and the whole thing goes through an SNES-style echo.
CHIMES = {
    'ready': (  # a bright rising G major flourish that settles on a held top note
        (0.00, 783.99, .30, 'p12', .7, .25), (0.07, 987.77, .30, 'p12', .7, .25),
        (0.14, 1174.66, .30, 'p12', .7, .25), (0.21, 1567.98, .50, 'p25', .9, .6),
        (0.00, 196.00, .65, 'tri', 1, .8)),
    'waiting': (  # a harp-like question: a rising Dmaj9 arpeggio left on its ninth, then a menu-cursor ping
        (0.00, 587.33, .45, 'p25', .6, .35), (0.07, 739.99, .45, 'p25', .6, .35),
        (0.14, 880.00, .45, 'p25', .6, .35), (0.21, 1318.51, .50, 'p25', .7, .25),
        (0.00, 146.83, .55, 'tri', 1, .6),
        (0.64, 1760.00, .08, 'p12', .8, .3), (0.72, 1318.51, .24, 'p12', .8, .3)),
    'failed': (  # game over: a slow A minor descent over a low drone, through the leading tone to a held A
        (0.00, 659.25, .20, 'p25', .7, 1), (0.20, 523.25, .20, 'p25', .7, 1), (0.40, 493.88, .20, 'p25', .7, 1),
        (0.60, 415.30, .26, 'p25', .7, 1), (0.86, 440.00, .70, 'p25', .8, .9),
        (0.00, 110.00, .60, 'tri', 1, 2), (0.60, 82.41, .26, 'tri', 1, 2), (0.86, 110.00, .70, 'tri', 1, 1.2)),
}
CHIME_DEFAULT = ('waiting', 'failed')  # Ready would chime after every turn; long tasks get a victory fanfare
CHIME_GAP = 60  # seconds before the same chat chimes the same way again
CHIME_VERSION = 3


def theme_for(name, cfg):
    """A character's theme: the file set with set-theme, else theme.<ext> in its folder. None if neither."""
    chosen = (cfg.get('themes') or {}).get(name)
    if chosen and Path(chosen).is_file():
        return Path(chosen)
    directory = (cfg.get('avatars') or {}).get(name)
    for ext in THEME_EXTS if directory else ():
        path = Path(directory) / ('theme' + ext)
        if path.is_file():
            return path
    return None


def pick_theme(rows, current, themes, now=0, picked=0, rotate=BACKGROUND_ROTATE):
    """Which chat's theme plays: only busy or background, unmuted chats whose character has a theme.
    Busy chats take priority. The one already playing keeps going; otherwise the chat whose turn started
    most recently takes over. With only background chats left, the music rotates to the next one every
    `rotate` seconds (since `picked`)."""
    live = [r for r in rows if not r.get('muted') and themes.get(r['id'])]
    eligible = [r for r in live if r['state'] in BUSY]
    if eligible:
        if any(r['id'] == current for r in eligible):
            return current
        best = max(eligible, key=lambda r: r.get('turn_started') or r.get('updated') or 0)
        return best['id']
    eligible = [r for r in live if r['state'] == 'background']
    ids = [r['id'] for r in eligible]
    if current in ids:
        if len(ids) > 1 and now - picked >= rotate:
            return ids[(ids.index(current) + 1) % len(ids)]
        return current
    best = max(eligible, key=lambda r: r.get('updated') or 0, default=None)
    return best and best['id']


MOODS = ('field', 'battle', 'boss')  # calmer to more urgent, by the pace usage is going at
MUSIC_KINDS = MOODS + ('victory',)
MUSIC_DEFAULTS = {'music_delay': 5, 'victory_after': 30, 'victory_seconds': 7,
                  'pace_calm': 80, 'pace_limit': 100, 'boss_left': 10}
# Session health: context share and size that make a chat long or too long, when an idle chat's prompt
# cache has gone cold, and what makes a running turn look stuck.
HEALTH_DEFAULTS = {'context_warn': 40, 'context_bad': 70, 'tokens_warn': 150_000, 'tokens_bad': 300_000,
                   'cache_ttl': 300, 'cold_tokens': 50_000, 'struggle_errors': 3,
                   'long_turn': 1200, 'long_turn_tools': 40}
SETTINGS = {**MUSIC_DEFAULTS, **HEALTH_DEFAULTS}
MOOD_MARGIN = 5  # points past a threshold before the mood changes, so estimates can't make it flicker
WINDOW_SECONDS = {'five_hour': 5 * 3600, 'seven_day': 7 * 86400}
BURN_SPAN = 1800  # the short window's pace comes from the last half hour of use
_music_specs = {}  # music.json path -> (mtime, parsed)


def setting(cfg, key):
    value = cfg.get(key, SETTINGS[key])
    return float(value) if isinstance(value, (int, float)) and value >= 0 else SETTINGS[key]


def listed_music(directory):
    """{'field': [...], 'battle': [...], 'boss': [...], 'victory': [...]} of existing files named in the
    folder's music.json, plus its optional 'victory_seconds'."""
    found = {kind: [] for kind in MUSIC_KINDS}
    path = Path(directory) / 'music.json' if directory else None
    try:
        stamp = path.stat().st_mtime
        if _music_specs.get(path, (None,))[0] != stamp:
            _music_specs[path] = (stamp, json.loads(path.read_text()))
        spec = _music_specs[path][1]
    except (AttributeError, OSError, ValueError):
        spec = {}
    if isinstance(spec, dict):
        for kind in MUSIC_KINDS:
            names = spec.get(kind)
            names = [names] if isinstance(names, str) else names if isinstance(names, list) else []
            files = (Path(directory) / n for n in names if isinstance(n, str))
            found[kind] = [f for f in files if f.suffix.lower() in THEME_EXTS and f.is_file()]
        seconds = spec.get('victory_seconds')
        if isinstance(seconds, (int, float)) and seconds > 0:
            found['victory_seconds'] = float(seconds)
    return found


def music_for(name, cfg):
    """A character's playlists from its music.json; without a field list there, the set-theme file
    (else theme.<ext>) is the field music."""
    found = listed_music((cfg.get('avatars') or {}).get(name))
    if not found['field']:
        theme = theme_for(name, cfg)
        found['field'] = [theme] if theme else []
    return found


def music_started(row, now, cfg):
    """Whether a chat's music plays yet: it is busy and its turn has run music_delay seconds, so short
    tasks stay silent. Per turn, not per state: Claude flips between thinking and tools every few seconds."""
    if row['state'] == 'background':  # its turn is over; the jobs it left running keep a quieter theme going
        return True
    return (row['state'] in BUSY and bool(row.get('turn_started'))
            and now - row['turn_started'] >= setting(cfg, 'music_delay'))


def usage_left(views):
    """The lowest percent left across a provider's usage windows; None without a reading."""
    lefts = [v['left'] for v in (views or {}).values() if v]
    return min(lefts) if lefts else None


def projected_use(view, span, now, recent=None):
    """Percent of a window used by its reset if the current pace holds: used now plus a burn rate (percent
    per second) times the time left. `recent` is the rate over the last half hour, given for the short
    window; otherwise the average since the window began, counted over at least a seventh of the window so
    one busy first hour isn't stretched across a whole week of nights."""
    used = 100 - view['left']
    resets = view.get('resets')
    if not resets or now >= resets:
        return used
    if recent is None:
        recent = used / max(now - (resets - span), span / 7)
    return used + max(0.0, recent) * (resets - now)


def recent_rate(history, now, span=BURN_SPAN):
    """Percent per second from (time, percent used) readings in the last `span` seconds; None with under
    five minutes of them, or across a reset."""
    recent = [(t, u) for t, u in history if now - t <= span]
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < 300 or recent[-1][1] < recent[0][1]:
        return None
    return (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])


def music_mood(projected, left, reached, previous, cfg):
    """The music's mood from a provider's pace: field while the projected use at reset stays under
    pace_calm (80%), battle on course to use nearly all of it, boss on course to run out before it resets,
    with under boss_left (10%) left, or once a limit is reached. A change waits until the projection is
    MOOD_MARGIN points past the threshold; without a reading the previous mood stays (field at first)."""
    if reached or (left is not None and left <= setting(cfg, 'boss_left')):
        return 'boss'
    if projected is None:
        return previous or 'field'
    calm, limit = setting(cfg, 'pace_calm'), setting(cfg, 'pace_limit')
    level = lambda x: 0 if x < calm else 1 if x <= limit else 2
    mood = level(projected)
    if previous in MOODS:
        before = MOODS.index(previous)
        if mood > before:
            mood = max(before, level(projected - MOOD_MARGIN))
        elif mood < before:
            mood = min(before, level(projected + MOOD_MARGIN))
    return MOODS[mood]


def anthropic_pace(state, views, now):
    """Projected use at reset per Anthropic window: the 5h window from this machine's last half hour of
    transcript tokens at the calibrated rate, the 7d window from its average pace."""
    out = {}
    for key, _ in WINDOWS:
        if not views.get(key):
            continue
        rate = (state.get('rates') or {}).get(key) if key == 'five_hour' else None
        recent = rate * weighted_between(now - BURN_SPAN, now) / BURN_SPAN if rate else None
        out[key] = projected_use(views[key], WINDOW_SECONDS[key], now, recent)
    return out


def codex_pace(reading, views, now, history=()):
    """Projected use at reset per Codex window: the short (primary) one from how fast recent readings
    climbed, when there are enough of them, else from its average pace."""
    out = {}
    for key, w in ((reading or {}).get('windows') or {}).items():
        if views.get(key):
            recent = recent_rate(history, now) if key == 'primary' else None
            out[key] = projected_use(views[key], (w.get('minutes') or (300 if key == 'primary' else 10080)) * 60,
                                     now, recent)
    return out


def pace_text(projected):
    return '' if projected is None else f' · pace {min(projected, 999):.0f}%'


def mood_tracks(songs, mood, bad=()):
    """The playable tracks for a mood, stepping down to a calmer one the character has no music for."""
    for kind in MOODS[MOODS.index(mood)::-1]:
        tracks = [t for t in songs.get(kind, ()) if t not in bad]
        if tracks:
            return kind, tracks
    return None, []


def next_track(slot, tracks, rng=random):
    """Shuffle-bag order: every track once per round, never the same one twice in a row across rounds."""
    bag = [t for t in slot.get('bag') or () if t in tracks]
    if not bag:
        bag = list(tracks)
        rng.shuffle(bag)
        if len(bag) > 1 and bag[0] == slot.get('track'):
            bag.append(bag.pop(0))
    slot.update(track=bag[0], bag=bag[1:], pos=0.0)
    return bag[0]


def chime_wav(kind, rate=22050):
    """One of the CHIMES jingles as 16-bit mono WAV bytes: pulse-wave melody over a triangle bass, notes
    ringing into each other, a slight vibrato on held notes, and a two-tap echo."""
    import io
    import wave
    waves = {'p25': lambda ph: 1.0 if ph < .25 else -1.0, 'p12': lambda ph: 1.0 if ph < .125 else -1.0,
             'tri': lambda ph: 4 * abs(ph - .5) - 1}
    notes = CHIMES[kind]
    dry = [0.0] * int(rate * max(start + ring for start, _, ring, *_ in notes))
    for start, freq, ring, shape, gain, decay in notes:
        offset, count, phase = int(rate * start), int(rate * ring), 0.0
        for i in range(min(count, len(dry) - offset)):
            t = i / rate
            wobble = 1 + .004 * math.sin(2 * math.pi * 5.5 * t) if ring >= .45 and t > .15 else 1
            phase = (phase + freq * wobble / rate) % 1
            env = min(1, t / .005) * min(1, (count - i) / (rate * .03)) * math.exp(-t / decay)  # click-free
            dry[offset + i] += waves[shape](phase) * env * gain
    taps = ((int(rate * .12), .32), (int(rate * .24), .14))  # SNES-style echo
    mixed = dry + [0.0] * taps[-1][0]
    for delay, gain in taps:
        for i, v in enumerate(dry):
            mixed[i + delay] += v * gain
    peak = max(1e-9, max(abs(v) for v in mixed))
    frames = bytearray()
    for v in mixed:
        frames += int(v / peak * .3 * 32767).to_bytes(2, 'little', signed=True)
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return out.getvalue()


def chime_path(kind):
    path = DATA / 'sounds' / f'{kind}-v{CHIME_VERSION}.wav'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(chime_wav(kind))
    return path


def child(command, **kwargs):
    """Start a process that dies with the monitor (PR_SET_PDEATHSIG), so a crashed or killed window never
    leaves a song looping or an SSH connection open."""
    import ctypes
    import signal
    prctl = ctypes.CDLL(None, use_errno=True).prctl  # looked up before fork: the child only makes the call
    kwargs.setdefault('stdout', subprocess.DEVNULL)
    kwargs.setdefault('stderr', subprocess.DEVNULL)
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, preexec_fn=lambda: prctl(1, signal.SIGTERM), **kwargs)


def play(command):
    return child(command)


def run_ui():
    import tkinter as tk
    from PIL import Image, ImageTk, ImageSequence

    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(DATA / 'ui.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    if not config().get('enabled', True):
        return
    root = tk.Tk()
    # The layout below is designed in pixels at 1x. Follow the display's DPI (Tk reports 96/72 at 96 DPI),
    # clamped so a misreported DPI stays usable; config.json's ui_scale overrides it.
    detected = min(2.5, max(1.0, float(root.tk.call('tk', 'scaling')) / (96 / 72)))
    ui = float(config().get('ui_scale') or detected)
    px = lambda n: int(round(n * ui))
    card_w, sprite_h = px(272), px(128)
    sprite_size = (px(168), px(126))
    frame_border = '#4a6fa5'  # the light steel-blue edge classic RPG dialogue windows use
    root.tk.call('tk', 'scaling', 96 / 72 * ui)  # fonts grow with the pixel layout, not the raw display DPI
    root.title('Agent Monitor')
    root.configure(bg='#151b29')
    root.attributes('-topmost', True)
    root.minsize(card_w + px(120), px(320))
    root.geometry(f'{px(420)}x{px(295)}+{max(0, root.winfo_screenwidth()-px(450))}+{max(0,root.winfo_screenheight()-px(360))}')
    header = tk.Frame(root, bg='#202a3d', padx=12, pady=9,
                      highlightthickness=2, highlightbackground=frame_border)
    header.pack(fill='x')
    title = tk.Label(header, text='AGENT MONITOR', fg='#dce7ff', bg='#202a3d', font=('Sans', 10, 'bold'))
    title.pack(side='left')
    verbose = [config().get('verbose', True)]
    ui_icons = {name: ImageTk.PhotoImage(draw_ui_icon(name, px(18)))
                for name in ('help', 'details', 'compact', 'sound', 'muted', 'clock', 'calendar',
                             'long', 'too long', 'cold', 'struggling')}
    def icon_button(name):
        return tk.Button(header, name=name, image=ui_icons['help'], relief='flat', bd=0, takefocus=0,
                         bg='#202a3d', activebackground='#303e57', padx=3, pady=3,
                         highlightthickness=1, highlightbackground=frame_border)
    help_button = icon_button('help')
    help_button.pack(side='right', padx=(6, 0))
    toggle = icon_button('details')
    toggle.pack(side='right', padx=(6, 0))
    master = icon_button('sound')
    master.pack(side='right', padx=(8, 0))
    count = tk.Label(header, fg='#aabbcf', bg='#202a3d', font=('Sans', 10))
    count.pack(side='right')
    drag = {}
    def drag_start(e):
        drag.update(x=e.x_root-root.winfo_x(), y=e.y_root-root.winfo_y())
    def drag_move(e):
        root.geometry(f'+{max(0,e.x_root-drag["x"])}+{max(0,e.y_root-drag["y"])}')
    for widget in (header, title, count):
        widget.bind('<Button-1>', drag_start)
        widget.bind('<B1-Motion>', drag_move)
    meter_bg = '#1a2233'
    gauges = tk.Frame(root, bg=meter_bg, padx=12, pady=6,
                      highlightthickness=2, highlightbackground=frame_border)
    gauges.pack(fill='x', pady=(6, 0))
    # One box per provider: a clock bar for the short (5h) window, a calendar bar for the weekly one.
    boxes, meters = {}, {}
    for provider in ('anthropic', 'openai'):
        box = tk.Frame(gauges, bg=meter_bg)
        who = tk.Label(box, bg=meter_bg, fg='#c2cce0', font=('Sans', 9), anchor='w')
        who.pack(fill='x', pady=(4 if provider == 'openai' else 0, 0))
        for stat, glyph in (('5h', 'clock'), ('7d', 'calendar')):
            line = tk.Frame(box, bg=meter_bg)
            line.pack(fill='x', pady=(3, 0))
            tk.Label(line, image=ui_icons[glyph], bg=meter_bg).pack(side='left')
            note = tk.Label(line, bg=meter_bg, fg='#aebbd3', font=('Monospace', 8), anchor='e', width=50)
            note.pack(side='right')
            gauge = tk.Canvas(line, height=px(10), bg='#151b29',
                              highlightthickness=1, highlightbackground=frame_border)
            gauge.pack(side='left', fill='x', expand=True, padx=(4, 6))
            meters[provider, stat] = (gauge, note)
        boxes[provider] = (box, who)
    boxes['anthropic'][0].pack(fill='x')
    def draw_meter(provider, stat, view, text):
        gauge, note = meters[provider, stat]
        note.configure(text=text)
        gauge.delete('all')
        width = gauge.winfo_width()
        if view and width > 1:
            left = view['left'] / 100
            fill = ('#84e0bb' if stat == '5h' else '#8bb6ff') if left > .5 else '#ffcf70' if left > .2 else '#ff919b'
            gauge.create_rectangle(0, 0, max(2, width * left), px(10), fill=fill, width=0)
            for i in range(1, 4):
                gauge.create_line(width * i / 4, 0, width * i / 4, px(10), fill='#202a3d')
    polling, who_cache, openai_cache, codex_polling = [False], [None, 0], [None, 0], [False]
    codex_history = []  # (fetched, percent used) of the short Codex window, for its recent pace
    def start_codex_poll():
        codex_polling[0] = True
        def work():
            try:
                poll_codex_limits()
            except Exception as e:  # keep the window alive; the reason lands in monitor.log
                print(f'Codex usage poll failed: {e!r}', file=sys.stderr, flush=True)
            finally:
                openai_cache[1] = 0  # redraw from the new reading on the next tick
                codex_polling[0] = False
        threading.Thread(target=work, daemon=True).start()
    def start_poll():
        polling[0] = True
        def work():
            try:
                poll_limits()
            except Exception as e:  # keep the window alive; the reason lands in monitor.log
                print(f'usage poll failed: {e!r}', file=sys.stderr, flush=True)
            finally:
                polling[0] = False
        threading.Thread(target=work, daemon=True).start()
    def update_gauges(rows, now):
        if now - who_cache[1] > 10:
            who_cache[:] = [account(), now]
        acct, state = who_cache[0], load_limits()
        expected = config().get('expected_org')
        who = boxes['anthropic'][1]
        if acct:
            text = ' · '.join(x for x in (acct['org'], acct['plan'] + (f' ({acct["tier"]})' if acct['tier'] else ''), acct['email']) if x)
            wrong = bool(expected) and acct['org'] != expected
            who.configure(text='ANTHROPIC · ' + ('(!) not ' + expected + ' · ' if wrong else '') + text,
                          fg='#ffcf70' if wrong else '#c2cce0')
        else:
            who.configure(text='ANTHROPIC · Claude account: not signed in', fg='#ffcf70')
        active = any(r['state'] in BUSY | {'waiting', 'background'} or now - (r['updated'] or 0) < POLL_SECONDS for r in rows)
        rolled = any(w.get('resets') and now > w['resets'] for w in (state.get('windows') or {}).values())
        switched = acct and state.get('account') != acct['email']
        if not polling[0] and (switched or (now - state.get('checked', 0) >= POLL_SECONDS
                                             and (active or rolled or not state.get('windows')))):
            start_poll()
        views = limit_view(state, now) if state.get('account') == (acct and acct['email']) else {}
        pace = anthropic_pace(state, views, now)
        for (key, span), stat in zip(WINDOWS, ('5h', '7d')):
            draw_meter('anthropic', stat, views.get(key),
                       f'{span} · ' + limit_text(views.get(key), state, now) + pace_text(pace.get(key)))
        usage['anthropic'] = (max(pace.values(), default=None), usage_left(views), False)
        if now - openai_cache[1] > 15:  # Codex also logs a fresh reading with each response
            openai_cache[:] = [codex_limits(now), now]
            short = ((openai_cache[0] or {}).get('windows') or {}).get('primary')
            if short and openai_cache[0].get('fetched'):
                stamp = openai_cache[0]['fetched']
                if not codex_history or codex_history[-1][0] < stamp:
                    codex_history.append((stamp, short['used']))
                del codex_history[:-120]
        reading = openai_cache[0]
        codex_state = load_codex_state() if not codex_polling[0] else {}
        reset_passed = any(w.get('resets') and now > w['resets'] for w in ((reading or {}).get('windows') or {}).values())
        if not codex_polling[0] and now - codex_state.get('checked', 0) >= (60 if reset_passed else POLL_SECONDS):
            start_codex_poll()
        box, who = boxes['openai']
        codex_views = codex_view(reading, now)
        codex_proj = codex_pace(reading, codex_views, now, codex_history)
        usage['openai'] = (max(codex_proj.values(), default=None), usage_left(codex_views),
                           bool((reading or {}).get('reached')))
        if not reading or not reading.get('windows'):
            if box.winfo_manager():
                box.pack_forget()
                resized[0] = True
            return
        if not box.winfo_manager():
            box.pack(fill='x')
            resized[0] = True
        extra = [f'Codex {reading["plan"].title()}' if reading.get('plan') else 'Codex']
        if reading.get('reached'):
            extra.append('limit reached')
        if reading.get('reset_credits'):
            n = reading['reset_credits']
            extra.append(f'{n} free reset' + ('' if n == 1 else 's'))
        who.configure(text='OPENAI · ' + ' · '.join(extra), fg='#ffcf70' if reading.get('reached') else '#c2cce0')
        views = codex_view(reading, now)
        for key, stat in (('primary', '5h'), ('secondary', '7d')):
            w = reading['windows'].get(key) or {}
            span = window_span(w.get('minutes'), stat)
            draw_meter('openai', stat, views.get(key),
                       f'{span} · ' + limit_text(views.get(key), reading, now) + pace_text(codex_proj.get(key)))
    canvas = tk.Canvas(root, bg='#151b29', highlightthickness=0)
    scroll = tk.Scrollbar(root, orient='vertical', command=canvas.yview)
    scroll.pack(side='right', fill='y')
    canvas.pack(fill='both', expand=True)
    canvas.configure(yscrollcommand=scroll.set)
    board = tk.Frame(canvas, bg='#151b29')
    board_id = canvas.create_window((0, 0), window=board, anchor='nw')
    board.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    empty =tk.Label(board, text='No active Claude Code or Codex chats\nAvatars appear when a chat opens.',
                     fg='#aabbcf', bg='#151b29', pady=45, font=('Sans', 11))
    cards, images = {}, {}
    cfg = config()
    last_ui_info = [None]
    resized = [True]
    thumbs, encounters, shifts, thumb_h = {}, {}, {}, px(54)
    def load_avatar(name, directory):
        for anim in ANIMATIONS:
            paths = scene_paths(directory, anim)
            encounters[name, anim] = [p.stem for p in paths]
            for path in paths:
                with Image.open(path) as im:
                    images[name, anim, path.stem] = [(ImageTk.PhotoImage(fit_sprite(frame, sprite_size)),
                                           max(40, frame.info.get('duration', 120))) for frame in ImageSequence.Iterator(im)]
        for path in variant_paths(directory, 'busy_shift')[:1]:
            key = (name, 'busy_shift', path.stem)
            with Image.open(path) as im:
                images[key] = [(ImageTk.PhotoImage(fit_sprite(frame, sprite_size)),
                                max(40, frame.info.get('duration', 120))) for frame in ImageSequence.Iterator(im)]
            shifts[name] = key
        with Image.open(gif_for(directory, 'idle')) as im:
            thumbs[name] = ImageTk.PhotoImage(fit_sprite(im, (px(72), px(54))))
    def reload_avatars():
        """Pick up avatars registered with add-avatar since the window opened."""
        for name, directory in config()['avatars'].items():
            if name not in thumbs:
                try:
                    load_avatar(name, directory)
                except (OSError, ValueError):
                    continue  # a broken folder stays out of the picker rather than crashing the window
                cfg['avatars'][name] = directory
    for name, directory in cfg['avatars'].items():
        load_avatar(name, directory)
    state_icons = {state: ImageTk.PhotoImage(draw_icon(state, px(20))) for state in LABELS}
    def card_h():
        return px(395 if verbose[0] else 245)
    def layout(event=None):
        width = max(card_w + px(16), canvas.winfo_width())
        canvas.itemconfigure(board_id, width=width)
        cols = min(3, max(1, width // (card_w + px(16))))
        for i, card in enumerate(cards.values()):
            card['frame'].grid(row=i//cols, column=i%cols, padx=7, pady=8, sticky='n')
        for i in range(cols):
            board.columnconfigure(i, weight=1)
        if not cards:
            empty.grid(row=0, column=0, columnspan=cols, sticky='ew')
        else:
            empty.grid_forget()
    canvas.bind('<Configure>', layout)
    canvas.bind('<Button-4>', lambda e: canvas.yview_scroll(-1, 'units'))
    canvas.bind('<Button-5>', lambda e: canvas.yview_scroll(1, 'units'))
    def show_details():
        toggle.configure(image=ui_icons['compact' if verbose[0] else 'details'])
        for card in cards.values():
            if verbose[0]:
                card['info'].pack(fill='x', pady=(4, 0))
            else:
                card['info'].pack_forget()
    def flip_details():
        verbose[0] = not verbose[0]
        c = config(); c['verbose'] = verbose[0]; save_config(c)
        show_details()
        resized[0] = True
    toggle.configure(command=flip_details)
    help_window = [None]
    def close_help(event=None):
        if help_window[0] is not None:
            help_window[0].destroy()
            help_window[0] = None
    def open_help():
        if help_window[0] is not None:  # the ? button toggles the popup
            close_help()
            return
        top = tk.Toplevel(root, name='help_popup', bg='#151b29')
        top.overrideredirect(True)  # no OS title bar: the drawn frame is the whole window
        top.attributes('-topmost', True)
        help_window[0] = top
        w, pad = px(780), px(24)
        c = tk.Canvas(top, width=w, height=700, bg='#151b29', highlightthickness=0, bd=0)
        c.pack()
        y = pad
        c.create_text(pad, y, anchor='nw', text='AGENT MONITOR', fill='#ffffff', font=('Sans', 12, 'bold'))
        y += px(32)
        for heading, body in HELP_SECTIONS:
            c.create_text(pad, y, anchor='nw', text=heading, fill='#ffd98a', font=('Sans', 9, 'bold'))
            y += px(19)
            if body is None:  # the state legend, one wrapped line per state, with the same icons the cards use
                for state, meaning in HELP_STATES.items():
                    c.create_image(pad + 2, y, anchor='nw', image=state_icons[state])
                    item = c.create_text(pad + px(30), y + 2, anchor='nw', fill='#f0f4ff', font=('Sans', 9),
                                         text=f'{LABELS[state]}  —  {meaning}', width=w - 2 * pad - px(30))
                    y = max(y + px(23), c.bbox(item)[3] + px(5))
            elif body == 'marks':  # the card marks, with the same icons
                for kind, meaning in HELP_MARKS:
                    c.create_image(pad + 2, y, anchor='nw', image=ui_icons[kind])
                    item = c.create_text(pad + px(32), y + 1, anchor='nw', fill='#f0f4ff', font=('Sans', 9),
                                         text=meaning, width=w - 2 * pad - px(32))
                    y = max(y + px(23), c.bbox(item)[3] + px(5))
                item = c.create_text(pad, y, anchor='nw', fill='#c2cce0', font=('Sans', 8), width=w - 2 * pad,
                                     text='These appear after a card\'s status; hover one for its numbers.')
                y = c.bbox(item)[3] + px(2)
            else:
                item = c.create_text(pad, y, anchor='nw', text=body, width=w - 2 * pad,
                                     fill='#f0f4ff', font=('Sans', 9))
                y = c.bbox(item)[3]
            y += px(13)
        ok = c.create_text(w - pad, y, anchor='ne', text='▶ OK', fill='#ffffff',
                           font=('Sans', 10, 'bold'), tags='ok')
        h = c.bbox(ok)[3] + pad
        c.configure(height=h)
        paint_window(c, w, h)
        c.tag_bind('ok', '<Button-1>', close_help)
        top.bind('<Escape>', close_help)
        top.bind('<Return>', close_help)
        x = root.winfo_rootx() + max(0, (root.winfo_width() - w) // 2)
        y = root.winfo_rooty() + 40
        x = max(0, min(x, root.winfo_screenwidth() - w))
        y = max(0, min(y, root.winfo_screenheight() - h))
        top.geometry(f'{w}x{h}+{x}+{y}')
        top.lift()
        top.focus_force()
    help_button.configure(command=open_help)
    root.bind('<Escape>', close_help)
    picker = [None, None]  # popup window, session it belongs to
    def close_picker(event=None):
        if picker[0] is not None:
            picker[0].destroy()
            picker[:] = [None, None]
    def choose(sid, name):
        close_picker()
        if sid in prefs:  # a remote chat: the choice is kept on this machine
            prefs[sid]['avatar'] = name
            save_prefs()
        else:
            with contextlib.closing(connect()) as db, db:
                db.execute('UPDATE sessions SET avatar=? WHERE id=?', (name, sid))
        if sid in cards:
            cards[sid]['row']['avatar'] = name
            cards[sid]['button'].configure(text=name + '  ▾')
    def open_picker(sid):
        if picker[0] is not None:  # the avatar button toggles its popup
            same = picker[1] == sid
            close_picker()
            if same:
                return
        reload_avatars()
        names = list(cfg['avatars'])
        row = cards[sid]['row']
        current = row['avatar'] if row['avatar'] in names else names[0]
        top = tk.Toplevel(root, name='avatar_popup', bg='#151b29')
        top.overrideredirect(True)
        top.attributes('-topmost', True)
        picker[:] = [top, sid]
        w, pad, line = px(260), px(20), thumb_h + px(10)
        c = tk.Canvas(top, width=w, height=600, bg='#151b29', highlightthickness=0, bd=0)
        c.pack()
        y = pad
        c.create_text(pad, y, anchor='nw', text='CHOOSE AVATAR', fill='#ffffff', font=('Sans', 11, 'bold'))
        y += 30
        for name in names:
            tag = 'pick%d' % names.index(name)
            c.create_rectangle(pad - 6, y - 4, w - pad + 6, y + line - 6, fill='', outline='', tags=(tag, tag + 'hi'))
            c.create_text(pad, y + thumb_h // 2, anchor='w', text='▶' if name == current else '',
                          fill='#ffffff', font=('Sans', 10, 'bold'), tags=tag)
            c.create_image(pad + 18, y, anchor='nw', image=thumbs[name], tags=tag)
            c.create_text(pad + 18 + thumb_h + 14, y + thumb_h // 2, anchor='w', text=name, tags=tag,
                          fill='#ffd98a' if name == current else '#f0f4ff', font=('Sans', 10, 'bold'))
            c.tag_bind(tag, '<Button-1>', lambda e, n=name: choose(sid, n))
            c.tag_bind(tag, '<Enter>', lambda e, t=tag: c.itemconfigure(t + 'hi', fill='#3a5ab0'))
            c.tag_bind(tag, '<Leave>', lambda e, t=tag: c.itemconfigure(t + 'hi', fill=''))
            y += line
        if len(names) == 1:
            item = c.create_text(pad, y + 4, anchor='nw', width=w - 2 * pad, fill='#aebbd3', font=('Sans', 8),
                                 text='No other avatars installed yet. Register one with add-avatar NAME DIRECTORY.')
            y = c.bbox(item)[3] + 6
        h = y + pad - 6
        c.configure(height=h)
        paint_window(c, w, h)
        top.bind('<Escape>', close_picker)
        button = cards[sid]['button']
        x = button.winfo_rootx() + (button.winfo_width() - w) // 2
        y = button.winfo_rooty() + button.winfo_height() + 4
        x = max(0, min(x, root.winfo_screenwidth() - w))
        y = max(0, min(y, root.winfo_screenheight() - h))
        top.geometry(f'{w}x{h}+{x}+{y}')
        top.lift()
        top.focus_force()
    def click_outside(event):
        if picker[0] is not None and picker[1] in cards and event.widget is not cards[picker[1]]['button']:
            close_picker()
    root.bind('<Button-1>', click_outside, add='+')
    root.bind('<Escape>', close_picker, add='+')
    sound = {'theme': None, 'key': None, 'proc': None, 'since': 0.0, 'seen': 0.0, 'states': {}, 'chimed': 0.0,
             'hold': 0.0, 'victory': None, 'bad': set(), 'moods': {}, 'per_chat': {}, 'picked': 0.0,
             'volume': None}
    usage = {}  # provider -> (lowest percent left, limit reached), kept by update_gauges for the music
    playlists = {}  # (avatar, kind) -> {'track', 'pos', 'bag'}: where each character's music left off
    players = {'chime': next(filter(None, map(shutil.which, ('pw-play', 'aplay', 'ffplay'))), None),
               'theme': shutil.which('ffplay')}
    def sound_on():
        return config().get('sound', True)
    def show_master():
        master.configure(image=ui_icons['sound' if sound_on() else 'muted'])
    def flip_master():
        c = config(); c['sound'] = not c.get('sound', True); save_config(c)
        show_master()
        audio(cards_rows(), time.time())
    master.configure(command=flip_master)
    def flip_mute(sid):
        muted = 0 if cards[sid]['row'].get('muted') else 1
        if sid in prefs:
            prefs[sid]['muted'] = muted
            save_prefs()
        else:
            with contextlib.closing(connect()) as db, db:
                db.execute('UPDATE sessions SET muted=? WHERE id=?', (muted, sid))
        cards[sid]['row']['muted'] = muted
        audio(cards_rows(), time.time())
    def cards_rows():
        return [card['row'] for card in cards.values() if 'row' in card]
    def stop_theme():
        if sound['proc'] and sound['proc'].poll() is None:
            sound['proc'].terminate()
            slot = playlists.get(sound['key'])
            if slot:  # resume here next time, so short turns walk through a long track instead of its intro
                slot['pos'] += time.time() - sound['since']
        sound.update(theme=None, key=None, proc=None)
    def stop_victory():
        if sound['victory'] and sound['victory'].poll() is None:
            sound['victory'].terminate()
        sound.update(victory=None, hold=0.0)
    def start_music(sid, key, tracks, volume, now):
        sound['volume'] = volume
        slot = playlists.setdefault(key, {'track': None, 'pos': 0.0, 'bag': []})
        if slot['track'] not in tracks:
            next_track(slot, tracks)
        sound.update(theme=sid, key=key, seen=now, since=now)
        command = [players['theme'], '-nodisp', '-autoexit', '-loglevel', 'quiet', '-volume', str(volume)]
        if slot['pos'] > 0:
            command += ['-ss', f'{slot["pos"]:.1f}', '-af', 'asetpts=PTS-STARTPTS,afade=t=in:d=1.5']
        try:
            sound['proc'] = play(command + [str(slot['track'])])
        except OSError as e:
            print(f'theme playback failed: {e!r}', file=sys.stderr, flush=True)
    def chime(kind):
        try:
            command = [players['chime'], str(chime_path(kind))]
            if command[0].endswith('ffplay'):
                command[1:1] = ['-nodisp', '-autoexit', '-loglevel', 'quiet']
            play(command)
        except OSError as e:
            print(f'chime failed: {e!r}', file=sys.stderr, flush=True)
    def audio(rows, now):
        on = sound_on()
        cfg_now = config()
        volume = max(0, min(100, int(cfg_now.get('theme_volume', 60))))
        quiet = max(0, min(100, int(cfg_now.get('background_volume', volume // 3))))  # background jobs, not a turn
        music = {r['avatar']: music_for(r['avatar'], cfg_now) for r in rows} if on and players['theme'] else {}
        if not on:
            stop_victory()
        proc = sound['proc']
        if proc is not None and proc.poll() is not None:  # the track ended (or ffplay could not play it)
            slot = playlists.get(sound['key'])
            if slot:
                if slot['pos'] == 0 and now - sound['since'] < 2:
                    sound['bad'].add(slot['track'])  # not retried until the monitor restarts
                slot['track'] = None  # the next start takes the next track in the bag
            sound.update(proc=None, key=None)
        # Ready after a battle-length turn plays the character's victory fanfare; otherwise chimes mark
        # chats that finished, need input, or failed.
        for row in rows:
            before = sound['states'].get(row['id'])
            sound['states'][row['id']] = row['state']
            if not (on and before is not None and before != row['state'] and row['state'] in CHIMES
                    and not row.get('muted') and now - sound['chimed'] > 1.5):
                continue
            last = sound['per_chat'].get((row['id'], row['state']), 0)
            sound['chimed'] = now
            songs = music.get(row['avatar'], {})
            wins = [t for t in songs.get('victory', ()) if t not in sound['bad']]
            if (row['state'] == 'ready' and wins and
                    (row.get('last_turn') or 0) >= setting(cfg_now, 'victory_after')):
                stop_theme()
                stop_victory()
                track = next_track(playlists.setdefault((row['avatar'], 'victory'), {}), wins)
                seconds = songs.get('victory_seconds') or setting(cfg_now, 'victory_seconds')
                sound['hold'] = now + seconds  # no other music over the fanfare
                try:
                    sound['victory'] = play([players['theme'], '-nodisp', '-autoexit', '-loglevel', 'quiet',
                                             '-volume', str(volume), '-t', f'{seconds:.1f}', '-af',
                                             f'afade=t=out:st={max(0, seconds - 1.5):.1f}:d=1.5', str(track)])
                except OSError as e:
                    print(f'victory playback failed: {e!r}', file=sys.stderr, flush=True)
            elif (players['chime'] and row['state'] in cfg_now.get('chimes', CHIME_DEFAULT)
                  and now - last >= CHIME_GAP):
                sound['per_chat'][row['id'], row['state']] = now
                chime(row['state'])
        # Background music: one chat at a time, its mood set by how much of its provider's usage is left.
        for provider, (projected, left, reached) in usage.items():
            sound['moods'][provider] = music_mood(projected, left, reached, sound['moods'].get(provider), cfg_now)
        keys = {}
        for row in rows:
            if not (music and music_started(row, now, cfg_now)):
                continue
            mood = sound['moods'].get('openai' if row.get('provider') == 'codex' else 'anthropic', 'field')
            kind, tracks = mood_tracks(music.get(row['avatar'], {}), mood, sound['bad'])
            if tracks:
                keys[row['id']] = ((row['avatar'], kind), tracks)
        target = pick_theme(rows, sound['theme'], keys, now, sound['picked']) if on and now >= sound['hold'] else None
        current = next((r for r in rows if r['id'] == sound['theme']), None)
        if target == sound['theme'] and target:
            sound['seen'] = now
        elif (target is None and current and on and not current.get('muted') and now >= sound['hold']
              and sound['key'] and now - sound['seen'] < THEME_GRACE):
            target = sound['theme']  # a short pause between turns
            keys[target] = (sound['key'], [playlists[sound['key']]['track']])
        key, tracks = keys.get(target, (None, None))
        chosen = next((r for r in rows if r['id'] == target), None)
        level = quiet if chosen and chosen['state'] == 'background' else volume
        if (target, key) != (sound['theme'], sound['key']) or (target and sound['proc'] is None) \
                or (target and level != sound['volume']):
            if target != sound['theme']:
                sound['picked'] = now
            stop_theme()
            if target and tracks:
                start_music(target, key, tracks, level, now)
        for sid in list(sound['states']):
            if sid not in cards:
                sound['states'].pop(sid)
        for sid, card in cards.items():
            muted = bool(card.get('row', {}).get('muted'))
            playing = sid == sound['theme'] and sound['proc'] is not None and sound['proc'].poll() is None
            card['mute'].configure(image=ui_icons['muted' if muted else 'sound'],
                                   bg='#4a6fa5' if playing else '#303e57')
    tip = [None]
    def show_tip(widget):
        hide_tip()
        if not getattr(widget, 'tip', None):
            return
        top = tk.Toplevel(root)
        top.overrideredirect(True)
        top.attributes('-topmost', True)
        tk.Label(top, text=widget.tip, bg='#0c1440', fg='#f0f4ff', font=('Sans', 8), padx=6, pady=3,
                 highlightthickness=1, highlightbackground='#d6deef', wraplength=260, justify='left').pack()
        top.geometry(f'+{widget.winfo_rootx() + 12}+{widget.winfo_rooty() + 22}')
        tip[0] = top
    def hide_tip(event=None):
        if tip[0] is not None:
            tip[0].destroy()
            tip[0] = None
    def make_card(sid):
        frame = tk.Frame(board, bg='#202a3d', padx=8, pady=7, width=card_w,
                         highlightthickness=2, highlightbackground=frame_border)
        sprite = tk.Label(frame, bg='#202a3d', height=sprite_h)
        sprite.pack()
        tag = tk.Label(frame, bg='#303e57', font=('Sans', 7, 'bold'), padx=4, pady=1)
        tag.place(relx=1.0, x=2, y=-1, anchor='ne')
        name_row = tk.Frame(frame, bg='#202a3d')
        name_row.pack(fill='x')
        button = tk.Button(name_row, command=lambda s=sid: open_picker(s), relief='flat', bg='#303e57', fg='#e7efff',
                           activebackground='#465a7a', activeforeground='white', font=('Sans', 10, 'bold'))
        button.pack(side='left', fill='x', expand=True)
        mute = tk.Button(name_row, name='mute', command=lambda s=sid: flip_mute(s), image=ui_icons['sound'],
                         relief='flat', bd=0, takefocus=0, bg='#303e57', activebackground='#465a7a', padx=5)
        mute.pack(side='right', fill='y', padx=(4, 0))
        label = tk.Label(frame, bg='#202a3d', fg='#c2cce0', font=('Sans', 9))
        label.pack(pady=(5, 0))
        status_row = tk.Frame(frame, bg='#202a3d')
        status_row.pack()
        icon = tk.Label(status_row, bg='#202a3d')
        icon.pack(side='left', padx=(0, 5))
        status = tk.Label(status_row, bg='#202a3d', font=('Sans', 10, 'bold'))
        status.pack(side='left')
        marks = {}
        for kind in ('health', 'cold', 'struggling'):  # shown only when they apply; hover says why
            marks[kind] = tk.Label(status_row, bg='#202a3d')
            marks[kind].bind('<Enter>', lambda e, w=marks[kind]: show_tip(w))
            marks[kind].bind('<Leave>', hide_tip)
        info = tk.Frame(frame, bg='#202a3d')
        bar = tk.Canvas(info, height=px(6), bg='#151b29', highlightthickness=0, width=card_w - px(16))
        text = tk.Label(info, bg='#202a3d', fg='#aebbd3', font=('Monospace', 8), justify='left', anchor='w')
        text.pack(fill='x')
        bar.pack(fill='x', pady=(3, 0))
        return dict(frame=frame, sprite=sprite, tag=tag, button=button, mute=mute, label=label, icon=icon, status=status,
                    info=info, text=text, bar=bar, marks=marks, key=None, index=0, due=0, shown=None,
                    tint_widgets=(frame, sprite, name_row, label, status_row, icon, status, *marks.values(), info, text))
    feeds, prefs, feed_info = {}, load_remote_prefs(), [None]
    def remote_sessions(now):
        wanted = {r['name']: r['target'] for r in config().get('remotes', [])
                  if isinstance(r, dict) and r.get('name') and r.get('target')}
        for name in list(feeds):
            if wanted.get(name) != feeds[name].target:
                feeds.pop(name).stop()
        for name, target in wanted.items():
            if name not in feeds:
                feeds[name] = RemoteFeed(name, target)
        rows = []
        for feed in feeds.values():
            rows += remote_rows(feed.name, feed.sessions, feed.offset, feed.online, prefs, list(cfg['avatars']))
        live = {r['id'] for r in rows}
        for sid in [k for k in prefs if k not in live and k.split('/', 1)[0] not in feeds]:
            prefs.pop(sid)  # a removed remote's choices
        save_prefs()
        info = {name: feed.status() for name, feed in feeds.items()}
        if info != feed_info[0]:
            atomic_json(DATA / 'remotes.json', info)
            feed_info[0] = info
        return rows
    def save_prefs():
        if prefs != load_remote_prefs():
            atomic_json(DATA / 'remote_prefs.json', prefs)
    def tick():
        if not config().get('enabled', True):
            root.destroy()
            return
        rows = refresh_sessions() + remote_sessions(time.time())
        cfg_live = config()
        ids = {r['id'] for r in rows}
        now = time.time()
        changed = resized[0]
        resized[0] = False
        for sid in list(cards):
            if sid not in ids:
                if picker[1] == sid:
                    close_picker()
                cards.pop(sid)['frame'].destroy()
                changed = True
        for row in rows:
            sid = row['id']
            if sid not in cards:
                cards[sid] = make_card(sid)
                changed = True
                show_details()
            card = cards[sid]
            if row.get('host'):
                usage = row['remote_usage']  # read and reconciled on the remote
            else:
                usage = session_usage(row)
                reconcile(row, usage)
            card['row'] = row
            if row['avatar'] not in cfg['avatars']:
                reload_avatars()
            avatar = row['avatar'] if row['avatar'] in cfg['avatars'] else next(iter(cfg['avatars']))
            anim = animation(row, now)
            key = choose_scene(card, avatar, anim, encounters[avatar, anim], now)
            key = choose_transition(card, avatar, anim, key, shifts.get(avatar), now)
            if card['key'] != key:
                card.update(key=key, index=0, due=0)
            card['button'].configure(text=avatar + '  ▾')
            # Same layout for every provider: chat name, then project folder (if any) and short ID.
            provider = 'codex' if row.get('provider') == 'codex' else 'claude'
            card['tag'].configure(text=PROVIDERS[provider].upper() + (f' · {row["host"]}' if row.get('host') else ''),
                                  fg='#8a93a6' if row.get('offline') else TAG_COLORS[provider])
            project = Path(row['cwd']).name if row['cwd'] else ''
            title = row.get('name') or (usage and usage['title'])
            short = row.get('short') or sid[:8]
            where = f'{project[:20]} · {short}' if project else short
            if title:
                title = title if len(title) <= 34 else title[:33].rstrip() + '…'
                card['label'].configure(text=f'{title}\n{where}')
            else:
                card['label'].configure(text=f'{project[:24]}\n{short}' if project else short)
            card['status'].configure(text=status_text(row) + (' · offline' if row.get('offline') else ''),
                                     fg='#8a93a6' if row.get('offline') else COLORS.get(row['state'], '#a6b0c3'))
            card['icon'].configure(image=state_icons.get(row['state'], state_icons['idle']))
            level, why = session_health(usage, cfg_live)
            flags = session_flags(row, usage, now, cfg_live)
            shown_marks = {'health': level in HEALTH_ADVICE and (level, f'{why}: {HEALTH_ADVICE[level]}'),
                           'cold': next((('cold', f) for f in flags if f.startswith('cache cold')), None),
                           'struggling': next((('struggling', f) for f in flags if not f.startswith('cache cold')), None)}
            for kind, mark in card['marks'].items():
                glyph = shown_marks[kind]
                if glyph:
                    mark.configure(image=ui_icons[glyph[0]])
                    mark.tip = glyph[1]
                    if not mark.winfo_manager():
                        mark.pack(side='left', padx=(5, 0))
                elif mark.winfo_manager():
                    mark.pack_forget()
            if verbose[0]:
                card['text'].configure(text='\n'.join(line[:38] for line in details(row, usage, now)))
                shown = round(min(1, usage['context'] / usage['window']), 3) if usage and usage['window'] else None
                if shown != card['shown']:
                    card['shown'] = shown
                    bar = card['bar']
                    bar.delete('all')
                    if shown is not None:
                        width = card_w - px(16)
                        fill = '#84e0bb' if shown < .5 else '#ffcf70' if shown < .8 else '#ff919b'
                        bar.create_rectangle(0, 0, max(2, width * shown), px(6), fill=fill, width=0)
        update_gauges(rows, now)
        audio(rows, now)
        visual_cfg = config()
        for row in rows:
            card = cards[row['id']]
            backdrop, border = card_palette(row, sound['moods'], visual_cfg)
            if card.get('backdrop') != backdrop:
                for widget in card['tint_widgets']:
                    widget.configure(bg=backdrop)
                card['backdrop'] = backdrop
            card['frame'].configure(highlightbackground=border)
        count.configure(text=f'{len(rows)} chat' + ('' if len(rows) == 1 else 's'))
        if changed:
            columns = min(max(1, len(rows)), 3, max(1, (root.winfo_screenwidth()-80)//(card_w+px(16))))
            width = max(px(360), columns*(card_w+px(16))+px(38))
            top = header.winfo_reqheight() + gauges.winfo_reqheight() + px(40)  # the OpenAI box comes and goes
            height = min(root.winfo_screenheight()-100, min(2, max(1, (len(rows)+columns-1)//columns))*card_h()+top)
            x = max(0, min(root.winfo_x(), root.winfo_screenwidth()-width-30))
            y = max(0, min(root.winfo_y(), root.winfo_screenheight()-height-50))
            root.geometry(f'{width}x{height}+{x}+{y}')
        if changed or not cards:
            layout()
        if root.winfo_ismapped():
            info = {'pid': os.getpid(), 'proc_start': process_info(os.getpid())['start'],
                    'geometry': [root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()]}
            if info != last_ui_info[0]:
                atomic_json(DATA / 'ui.json', info)
                last_ui_info[0] = info
        root.after(700, tick)
    def animate():
        now = time.monotonic()
        for card in cards.values():
            if card['key'] and now >= card['due']:
                frames = images[card['key']]
                if card.get('restore_key') and card['index'] >= len(frames):
                    card.update(key=card.pop('restore_key'), index=0)
                    frames = images[card['key']]
                image, duration = frames[card['index'] % len(frames)]
                card['sprite'].configure(image=image)
                card['index'] += 1
                card['due'] = now + duration / 1000
        root.after(40, animate)
    def close():
        c = config()
        c['enabled'] = False
        save_config(c)
        root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    try:
        show_details()
        show_master()
        tick()
        animate()
        root.update_idletasks()
        atomic_json(DATA / 'ui.json', {'pid': os.getpid(), 'proc_start': process_info(os.getpid())['start'],
                                     'geometry': [root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()]})
        root.mainloop()
    finally:
        stop_theme()
        stop_victory()
        for feed in feeds.values():
            feed.stop()
        (DATA / 'remotes.json').unlink(missing_ok=True)
        (DATA / 'ui.json').unlink(missing_ok=True)
        lock.close()


SNAPSHOT_VERSION = 1
SNAPSHOT_EVERY = 3  # seconds between a remote's snapshots
REMOTE_SCRIPT = '~/.agent-monitor/monitor.py'  # where remote-add copies this script on a remote host
SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new']
SSH = ['ssh'] + SSH_OPTIONS + ['-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
# Never prompts: keys, ssh-agent and ~/.ssh/config only; a new host key is accepted once, a changed one refused.
REMOTE_TIMES = ('created', 'started', 'updated', 'turn_started', 'flash_until')


def snapshot():
    """This machine's sessions with their transcript usage: what a remote host sends the window over SSH."""
    sessions = []
    for row in refresh_sessions():
        usage = session_usage(row)
        reconcile(row, usage)
        sessions.append(dict(row, usage=usage and dict(usage, finished=len(usage['finished']))))
    return {'version': SNAPSHOT_VERSION, 'now': time.time(), 'sessions': sessions}


def watch_snapshots(every=SNAPSHOT_EVERY):
    """Print a snapshot line whenever the sessions change (and every 30 s regardless) until the reader goes.
    One long-lived process keeps the transcript readers warm instead of re-reading every file per poll."""
    last, sent = None, 0.0
    try:
        while os.getppid() != 1:  # sshd went away
            snap = snapshot()
            body = json.dumps(snap['sessions'], sort_keys=True)
            if body != last or time.time() - sent > 30:
                print(json.dumps(snap, separators=(',', ':')), flush=True)
                last, sent = body, time.time()
            time.sleep(every)
    except (BrokenPipeError, KeyboardInterrupt):
        pass


def remote_rows(host, sessions, offset, online, prefs, names):
    """A remote's snapshot sessions as local card rows: IDs namespaced by host, times moved onto this clock,
    and the avatar and mute chosen on this machine (new chats rotate through the local catalog)."""
    rows = []
    for session in sessions:
        row = dict(session, id=f'{host}/{session["id"]}', host=host, short=str(session['id'])[:8],
                   offline=not online, remote_usage=session.get('usage'))
        for key in REMOTE_TIMES:
            if isinstance(row.get(key), (int, float)):
                row[key] += offset
        pref = prefs.setdefault(row['id'], {})
        if pref.get('avatar') not in names and names:
            pref['avatar'] = names[sum(1 for p in prefs.values() if p.get('avatar')) % len(names)]
        row.update(avatar=pref.get('avatar') or row.get('avatar'), muted=int(bool(pref.get('muted'))))
        rows.append(row)
    return rows


def load_remote_prefs():
    try:
        prefs = json.loads((DATA / 'remote_prefs.json').read_text())
        return prefs if isinstance(prefs, dict) else {}
    except (OSError, ValueError):
        return {}


class RemoteFeed:
    """One remote host's sessions over a single long-lived SSH connection running `snapshot watch` there.
    A dropped connection retries with backoff (5 s, doubling to a minute); the host's last sessions stay
    on screen, marked offline, until it answers again."""
    def __init__(self, name, target):
        self.name, self.target = name, target
        self.sessions, self.offset, self.online, self.error, self.seen = [], 0.0, False, None, 0.0
        self.proc, self.stopped = None, False
        threading.Thread(target=self.run, daemon=True).start()

    def command(self):
        return SSH + [check_ssh_target(self.target), f'python3 {REMOTE_SCRIPT} snapshot watch']

    def run(self):
        delay = 5
        while not self.stopped:
            began, log = time.time(), DATA / f'remote-{self.name}.log'
            try:
                with open(log, 'w') as errors:
                    self.proc = child(self.command(), stdout=subprocess.PIPE, stderr=errors, text=True)
                    for line in self.proc.stdout:
                        snap = json.loads(line)
                        if snap.get('version') != SNAPSHOT_VERSION:
                            raise ValueError('its monitor is a different version: rerun remote-add')
                        self.offset = time.time() - snap['now']  # remote clock to this one
                        self.sessions, self.online, self.error, self.seen = snap['sessions'], True, None, time.time()
                        delay = 5
                    self.proc.wait()
                lines = log.read_text().strip().splitlines()
                self.error = lines[-1][:120] if lines else f'disconnected (exit {self.proc.returncode})'
            except (OSError, ValueError, KeyError, TypeError) as e:
                self.error = str(e)[:120]
                if self.proc and self.proc.poll() is None:
                    self.proc.terminate()
            self.online = False
            if time.time() - began > 60:
                delay = 5  # a long-lived connection that dropped retries soon
            for _ in range(int(delay * 10)):
                if self.stopped:
                    return
                time.sleep(.1)
            delay = min(60, delay * 2)

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def status(self):
        return {'target': self.target, 'online': self.online, 'error': self.error, 'sessions': len(self.sessions),
                'last_seen': self.seen or None}


def remote_status():
    """Each registered remote and, while the window runs, its connection state."""
    try:
        live = json.loads((DATA / 'remotes.json').read_text())
    except (OSError, ValueError):
        live = {}
    return {r['name']: live.get(r['name'], {'target': r['target'], 'online': None, 'error': 'window not running'})
            for r in config().get('remotes', []) if isinstance(r, dict) and r.get('name')}


def check_ssh_target(target):
    if not target or target.startswith('-') or any(c.isspace() for c in target):
        raise ValueError(f'{target!r} is not an SSH target or ~/.ssh/config alias')
    return target


def remote_add(target, name=None):
    """Copy this script to a host, install its hooks there in headless mode, and register the host."""
    target, name = check_ssh_target(target), name or target
    if not re.fullmatch(r'[\w.@-]{1,40}', name):
        raise ValueError(f'{name!r}: use up to 40 letters, digits, ".", "-", "_" or "@" for a remote name')
    run = lambda command, **kw: subprocess.run(SSH + [target, command], text=True, capture_output=True, timeout=60, **kw)
    found = run('mkdir -p ~/.agent-monitor && python3 -c "import sys; print(sys.version_info >= (3, 9))"')
    if found.returncode != 0:
        raise RuntimeError(f'ssh {target} failed: {(found.stderr.strip().splitlines() or ["no error output"])[-1]}')
    if found.stdout.strip() != 'True':
        raise RuntimeError(f'{target} needs Python 3.9 or newer as python3')
    copied = subprocess.run(['scp', '-q'] + SSH_OPTIONS + [str(Path(__file__).resolve()),
                             f'{target}:.agent-monitor/monitor.py'], text=True, capture_output=True, timeout=60)
    if copied.returncode != 0:
        raise RuntimeError(f'copy to {target} failed: {copied.stderr.strip()}')
    setup = run(f'python3 {REMOTE_SCRIPT} remote-setup')
    if setup.returncode != 0:
        raise RuntimeError(f'setup on {target} failed: {setup.stderr.strip() or setup.stdout.strip()}')
    c = config()
    c['remotes'] = [r for r in c.get('remotes', []) if r.get('name') != name] + [{'name': name, 'target': target}]
    save_config(c)
    return setup.stdout.strip()


def remote_setup():
    """Run on a remote host by remote-add: record hook events there, never open a window."""
    c = config(); c['headless'] = True; save_config(c)
    done = []
    if (Path.home() / '.claude').is_dir() or shutil.which('claude'):
        install_hooks()
        done.append('Claude Code')
    if CODEX.is_dir():
        install_codex_hooks()
        done.append('Codex (approve once in Codex: /hooks)')
    print('Hooks installed for ' + (', '.join(done) if done else 'nothing yet: install Claude Code or Codex, then rerun remote-add'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start','stop','status','run','hook','install-hooks','uninstall-hooks',
                                            'install-codex-hooks','uninstall-codex-hooks','add-avatar','set-theme',
                                            'snapshot','remote-add','remote-remove','remote-setup'])
    parser.add_argument('extra', nargs='*')
    args = parser.parse_args()
    if args.command == 'hook':
        provider = 'codex' if args.extra == ['codex'] else 'claude'
        payload = {}
        try:
            payload = json.load(sys.stdin)
            record(payload, provider=provider)
            if payload.get('hook_event_name') != 'SessionEnd':
                launch()
        except Exception:
            # Observability must never block the agent or inject text into its context.
            pass
        if provider == 'codex' and isinstance(payload, dict) and payload.get('hook_event_name') in CODEX_JSON_EVENTS:
            print('{}')
    elif args.command == 'run':
        run_ui()
    elif args.command == 'snapshot':
        if args.extra == ['watch']:
            watch_snapshots()
        else:
            print(json.dumps(snapshot(), indent=2))
    elif args.command == 'remote-setup':
        remote_setup()
    elif args.command == 'remote-add':
        if len(args.extra) not in (1, 2):
            parser.error('remote-add requires TARGET [NAME]: an SSH target or ~/.ssh/config alias that works without a password')
        try:
            print(remote_add(*args.extra))
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            parser.exit(1, f'remote-add: {e}\n')
        print(f'Added {args.extra[-1]}. Its chats appear in the window within a few seconds.')
    elif args.command == 'remote-remove':
        if len(args.extra) != 1:
            parser.error('remote-remove requires NAME')
        c = config()
        entry = next((r for r in c.get('remotes', []) if r.get('name') == args.extra[0]), None)
        if not entry:
            parser.error(f'No remote named {args.extra[0]}; registered: {", ".join(r["name"] for r in c.get("remotes", [])) or "none"}')
        try:
            out = subprocess.run(SSH + [check_ssh_target(entry['target']),
                                        f'python3 {REMOTE_SCRIPT} uninstall-hooks; python3 {REMOTE_SCRIPT} uninstall-codex-hooks'],
                                 text=True, capture_output=True, timeout=60)
            note = 'its hooks were removed' if out.returncode == 0 else f'its hooks could not be removed ({out.stderr.strip() or "unreachable"})'
        except (OSError, subprocess.TimeoutExpired) as e:
            note = f'its hooks could not be removed ({e})'
        c['remotes'] = [r for r in c['remotes'] if r is not entry]
        save_config(c)
        print(f'Removed {entry["name"]}; {note}. Files in ~/.agent-monitor on it were left in place.')
    elif args.command == 'start':
        c = config(); c['enabled'] = True; save_config(c)
        launch(wait=True)
        print('Agent monitor running.')
    elif args.command == 'stop':
        c = config(); c['enabled'] = False; save_config(c)
        for _ in range(30):
            if not ui_status():
                break
            time.sleep(.1)
        if ui_status():
            raise RuntimeError('Monitor has not stopped yet; inspect monitor.log before restarting')
        print('Monitor stopped; automatic launch disabled until start.')
    elif args.command == 'status':
        now, sessions = time.time(), refresh_sessions()
        for row in sessions:
            reader = session_usage(row)
            while reader and reader['loading']:
                reader = session_usage(row)
            reconcile(row, reader)
            if reader:
                reader = dict(reader, finished=len(reader['finished']))
            level, why = session_health(reader, config())
            row.update(label=status_text(row), usage=reader, details=details(row, reader, now),
                       health=level and {'level': level, 'why': why, 'advice': HEALTH_ADVICE.get(level)},
                       flags=session_flags(row, reader, now, config()))
        state = load_limits()
        if now - state.get('checked', 0) >= POLL_SECONDS or state.get('account') != (account() or {}).get('email'):
            state = poll_limits(now)
        views = limit_view(state, now)
        pace = {'anthropic': anthropic_pace(state, views, now)}
        moods = {'anthropic': music_mood(max(pace['anthropic'].values(), default=None), usage_left(views), False, None, config())}
        limits = {span: limit_text(views.get(key), state, now) for key, span in WINDOWS}
        limits.update({f'{span}_pace': round(pace['anthropic'][key], 1) for key, span in WINDOWS if key in pace['anthropic']})
        limits.update(source=state.get('source'), error=state.get('error'), other=state.get('extra'),
                      calibrated=sorted(state.get('rates') or {}))
        if now - load_codex_state().get('checked', 0) >= POLL_SECONDS:
            poll_codex_limits(now)
        openai = codex_limits(now)
        codex_proj = codex_pace(openai, codex_view(openai, now), now)
        moods['openai'] = music_mood(max(codex_proj.values(), default=None), usage_left(codex_view(openai, now)),
                                     bool((openai or {}).get('reached')), None, config())
        if openai and openai.get('windows'):
            views = codex_view(openai, now)
            openai = dict({window_span(w['minutes'], key): limit_text(views.get(key), openai, now)
                           for key, w in openai['windows'].items()},
                          plan=openai['plan'], source=openai['source'], reached=openai.get('reached'),
                          reset_credits=openai.get('reset_credits'), error=openai.get('error'))
        cfg = config()
        music = {n: music_for(n, cfg) for n in cfg['avatars']}
        sound = {'on': cfg.get('sound', True),
                 'music': {n: {k: [f.name for f in v[k]] for k in MUSIC_KINDS} for n, v in music.items()},
                 'settings': {k: setting(cfg, k) for k in SETTINGS}, 'moods': moods,
                 'theme_player': shutil.which('ffplay'), 'muted_sessions': [r['id'] for r in sessions if r.get('muted')]}
        print(json.dumps({'running': bool(ui_status()), 'enabled': config().get('enabled', True), 'sound': sound,
                          'account': account(), 'limits': limits, 'openai_limits': openai,
                          'avatars': list(config()['avatars']), 'sessions': sessions,
                          'remotes': remote_status()}, indent=2))
    elif args.command in ('install-hooks', 'uninstall-hooks'):
        install_hooks(args.command == 'uninstall-hooks')
    elif args.command in ('install-codex-hooks', 'uninstall-codex-hooks'):
        install_codex_hooks(args.command == 'uninstall-codex-hooks')
    elif args.command == 'add-avatar':
        if len(args.extra) != 2:
            parser.error('add-avatar requires NAME DIRECTORY')
        from PIL import Image
        name, directory = args.extra[0], Path(args.extra[1]).resolve()
        if not name.strip() or len(name) > 60:
            parser.error('Avatar name must contain 1–60 characters')
        for state in ANIMATIONS:
            for path in scene_paths(directory, state):
                with Image.open(path) as im:
                    im.verify()
        c = config(); c['avatars'][name] = str(directory); save_config(c)
        print(f'Added {name}. It appears in the avatar picker the next time you open it.')
    elif args.command == 'set-theme':
        if len(args.extra) not in (1, 2):
            parser.error('set-theme requires NAME [FILE]; without FILE the theme is cleared')
        c = config()
        name = args.extra[0]
        if name not in c['avatars']:
            parser.error(f'No avatar named {name}; installed: {", ".join(c["avatars"])}')
        themes = c.setdefault('themes', {})
        if len(args.extra) == 1:
            themes.pop(name, None)
            save_config(c)
            print(f'Cleared {name}\'s theme.' + (f' Its folder still has {theme_for(name, c).name}.' if theme_for(name, c) else ''))
            return
        path = Path(args.extra[1]).expanduser().resolve()
        if not path.is_file():
            parser.error(f'{path} is not a file')
        if path.suffix.lower() not in THEME_EXTS:
            parser.error(f'Theme must be one of {", ".join(THEME_EXTS)}')
        themes[name] = str(path)
        save_config(c)
        print(f'{name} will play {path.name} while its chat works.'
              + (' Its music.json field playlist still takes precedence.' if listed_music(c['avatars'][name])['field'] else '')
              + ('' if shutil.which('ffplay') else ' ffplay is not installed, so nothing can play yet.'))


if __name__ == '__main__':
    main()
