"""Codex aliases, wrapped calls and scene selection without live chats or a display."""
import importlib.util
import json
from pathlib import Path
import random
import tempfile
import unittest


spec = importlib.util.spec_from_file_location('monitor_tool_tests',
                                            Path(__file__).resolve().parents[1] / 'scripts/monitor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ToolStateTests(unittest.TestCase):
    def test_namespaced_actions(self):
        cases = {
            'Read': 'reading', 'Grep': 'reading', 'functions.view_image': 'reading',
            'web.run': 'reading', 'web__run': 'reading',
            'mcp__codex_apps__search_service__search': 'reading',
            'functions.read_mcp_resource': 'reading', 'collaboration.list_agents': 'reading',
            'mcp__docs__get_document': 'reading', 'clock__curr_time': 'reading',
            'functions.apply_patch': 'working', 'mcp__docs__update_document': 'working',
            'image_gen__imagegen': 'working', 'mcp__node_repl.js': 'working',
            'functions.wait': 'thinking', 'clock.sleep': 'thinking',
            'functions.update_plan': 'thinking', 'functions.create_goal': 'thinking',
            'collaboration.spawn_agent': 'delegating', 'collaboration.wait_agent': 'delegating',
            'collaboration.send_message': 'delegating', 'Agent': 'delegating',
            'functions.request_user_input_async': 'waiting', 'AskUserQuestion': 'waiting',
            'unknown_tool': 'working', None: 'working',
        }
        for name, state in cases.items():
            with self.subTest(name=name):
                self.assertEqual(m.tool_state(name), state)

    def test_read_commands_and_mixed_work(self):
        for command in ('rg --files', 'pwd && rg -n TODO src', 'cat README.md | head -20',
                        "sed -n '1,40p' README.md", 'git status --short && git diff --stat'):
            with self.subTest(command=command):
                self.assertEqual(m.tool_state('functions.exec_command', {'cmd': command}), 'reading')
        for command in ('cat README.md && rm old.txt', 'git add .', 'python3 build.py',
                        'cat README.md > copy.txt', 'cat "$(touch flag)"', 'cat README.md\nrm old.txt',
                        "sed -i 's/a/b/' file", "sed 'w /tmp/out' file", "sed -n '1p' -e 'w /tmp/out' file",
                        'rg --pre converter text',
                        'git diff --output=report.txt', 'cat README.md &'):
            with self.subTest(command=command):
                self.assertEqual(m.tool_state('exec_command', {'cmd': command}), 'working')
        self.assertEqual(m.tool_state('Bash', {'command': 'cat README.md'}), 'reading')
        self.assertEqual(m.tool_state('exec_command', json.dumps({'cmd': 'rg --files'})), 'reading')

    def test_exec_wrappers_and_literals(self):
        self.assertEqual(m.tool_activity('functions.exec',
            'text(await tools.exec_command({cmd:"rg --files"}));'), ('reading', 'rg'))
        self.assertEqual(m.tool_state('exec', {'code':
            'await tools.web__run({search_query:[]}); await tools.view_image({path:"image.png"});'}), 'reading')
        self.assertEqual(m.tool_state('exec', json.dumps({'code':
            'await tools.view_image({path:"image.png"});'})), 'reading')
        self.assertEqual(m.tool_state('exec',
            'await tools.view_image({path:"image.png"}); await tools.apply_patch("patch");'), 'working')
        self.assertEqual(m.tool_state('exec', 'await tools.request_user_input_async({questions:[]});'), 'waiting')
        self.assertEqual(m.tool_state('exec', 'await tools.clock__sleep({duration_ms:100});'), 'thinking')
        self.assertEqual(m.tool_state('exec',
            '// tools.apply_patch("example")\nawait tools.view_image({path:"image.png"});'), 'reading')
        self.assertEqual(m.tool_state('exec',
            'text("tools.request_user_input_async({})"); await tools.view_image({path:"image.png"});'), 'reading')
        self.assertEqual(m.tool_state('exec',
            'await tools.view_image({path:"image.png"}); await tools[name]({});'), 'working')
        self.assertEqual(m.tool_state('exec', 'await tools.exec_command({cmd: computed});'), 'working')

    def test_hook_transitions_keep_only_resolved_names(self):
        row = {'state': 'thinking'}
        source = 'await tools.exec_command({cmd:"rg --files private-project-marker"});'
        m.apply(row, 'PreToolUse', {'tool_name': 'exec', 'tool_input': source}, 100)
        self.assertEqual(row['state'], 'reading')
        self.assertEqual(row['tool'], 'rg')
        self.assertNotIn('private-project-marker', json.dumps(row))
        m.apply(row, 'PostToolUse', {}, 101)
        self.assertEqual(row['state'], 'thinking')
        m.apply(row, 'PreToolUse', {'tool_name': 'functions.wait'}, 102)
        self.assertEqual((row['state'], row['waiters']), ('thinking', '[]'))
        m.apply(row, 'PreToolUse', {'tool_name': 'collaboration.wait_agent'}, 103)
        self.assertEqual((row['state'], row['waiters']), ('delegating', '[]'))
        m.apply(row, 'PreToolUse', {'tool_name': 'functions.request_user_input'}, 104)
        self.assertEqual((row['state'], row['waiters']), ('waiting', '[""]'))
        m.apply(row, 'PreToolUse', {'tool_name': 'functions.apply_patch'}, 105)
        self.assertEqual((row['state'], row['waiters']), ('working', '[]'))
        self.assertEqual(m.status_text({'state': 'reading', 'source': 'hook',
            'tool': 'mcp__very_long_server_name__search_files'}), 'Reading · search_files')
        self.assertEqual(m.status_text({'state': 'reading', 'source': 'hook',
            'tool': 'web.run'}), 'Reading · web.run')
        m.apply(row, 'PreToolUse', {'tool_name': 'collaboration.spawn_agent',
            'tool_input': {'run_in_background': True}, 'tool_use_id': 'spawn'}, 106)
        self.assertEqual((row['state'], row['jobs']), ('delegating', '{}'))
        m.apply(row, 'PreToolUse', {'tool_name': 'functions.Monitor', 'tool_use_id': 'watch'}, 107)
        self.assertEqual(json.loads(row['jobs']), {'watch': 107})

    def test_activity_rotations_and_fixed_alerts(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for name in ('study', 'think', 'summon', 'summon-two', 'combat', 'alert'):
                (directory / (name + '.gif')).touch()
            (directory / 'animations.json').write_text(json.dumps({
                'reading': ['study'], 'thinking': ['think'], 'delegating': ['summon'],
                'working': ['combat'], 'waiting': ['alert'], 'failed': ['alert'], 'idle': ['alert']}))
            (directory / 'variants.json').write_text(json.dumps({
                'reading': ['study', 'think'], 'thinking': ['think', 'study'],
                'delegating': ['summon', 'summon-two'], 'waiting': ['combat']}))
            for state in ('reading', 'thinking', 'delegating'):
                choices = m.scene_paths(directory, state)
                self.assertEqual(len(choices), 2)
                self.assertNotIn(directory / 'combat.gif', choices)
                card = {}
                rng = random.Random(7)
                first = m.choose_scene(card, 'avatar', state, choices, 0, rng)
                self.assertEqual(m.choose_scene(card, 'avatar', state, choices, 1, rng), first)
                second = m.choose_scene(card, 'avatar', state, choices, 30, rng)
                self.assertNotEqual(first, second)
            for state in ('waiting', 'failed', 'idle'):
                self.assertEqual(m.scene_paths(directory, state), [directory / 'alert.gif'])


if __name__ == '__main__':
    unittest.main()
