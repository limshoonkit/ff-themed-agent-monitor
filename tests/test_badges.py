"""Subagent and remote-host badges without live chats or a display."""
import importlib.util
import json
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location('monitor_badge_tests',
                                            Path(__file__).resolve().parents[1] / 'scripts/monitor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class HostTests(unittest.TestCase):
    def test_commands_keep_only_the_host(self):
        cases = {
            'ssh -p 2222 -i key me@10.0.0.5 "rm -rf /tmp/x"': [('10.0.0.5', 'ssh')],
            'sudo -E timeout 30 ssh -J jump box1 ls': [('box1', 'ssh')],
            'scp a.txt devbox:/tmp/ && rsync -av ./ gpu:/x/': [('devbox', 'scp'), ('gpu', 'rsync')],
            'curl -s https://api.github.com/x?token=secret | jq .': [('api.github.com', 'curl')],
            'git clone git@github.com:o/r.git': [('github.com', 'git')],
            'psql -h db.internal -U x': [('db.internal', 'psql')],
            'nc -zv 192.168.1.4 22': [('192.168.1.4', 'nc')],
        }
        for command, hosts in cases.items():
            with self.subTest(command=command):
                self.assertEqual(m.command_hosts(command), hosts)

    def test_local_dynamic_and_unrelated_commands_are_ignored(self):
        for command in ('curl http://localhost:8080', 'curl http://127.0.0.2/', 'ssh $HOST ls',
                        'echo https://example.com', 'cat a | grep b', 'scp ./a:b c', 'cp a b'):
            with self.subTest(command=command):
                self.assertEqual(m.command_hosts(command), [])

    def test_tools_and_codex_shapes(self):
        self.assertEqual(m.tool_hosts('WebFetch', {'url': 'https://docs.python.org/3/'}),
                         [('docs.python.org', 'WebFetch')])
        self.assertEqual(m.tool_hosts('exec_command', {'cmd': ['bash', '-lc', 'wget http://mirror.lan/x']}),
                         [('mirror.lan', 'wget')])
        self.assertEqual(m.tool_hosts('exec', 'await tools.exec_command({cmd: "ssh devbox uptime"})'),
                         [('devbox', 'ssh')])
        self.assertEqual(m.tool_hosts('Read', {'file_path': '/x'}), [])

    def test_hook_records_hosts_and_badge(self):
        row = m.blank('s')
        m.apply(row, 'PreToolUse', {'tool_name': 'Bash', 'tool_input': {'command': 'curl https://a.example/x'}}, 100)
        icon, label, tip = m.host_badge(row, 110)
        self.assertEqual((icon, label), ('remote', 'a.example'))
        self.assertNotIn('/x', row['hosts'])
        m.apply(row, 'PreToolUse', {'tool_name': 'Bash', 'tool_input': {'command': 'ssh gpu nvidia-smi'},
                                    'agent_id': 'sub1'}, 120)
        icon, label, tip = m.host_badge(row, 130)
        self.assertEqual((icon, label), ('remote shell', 'gpu +1'))
        self.assertIn('a.example · curl', tip)
        self.assertNotIn('nvidia-smi', row['hosts'])
        self.assertIsNone(m.host_badge(row, 120 + m.HOST_TTL + 1))

    def test_tracking_can_be_turned_off(self):
        row = m.blank('s')
        m.apply(row, 'PreToolUse', {'tool_name': 'Bash', 'tool_input': {'command': 'ssh gpu'}}, 100, track_hosts=False)
        self.assertIsNone(m.host_badge(row, 101))


class AgentBadgeTests(unittest.TestCase):
    def test_each_subagent_shows_type_and_tool(self):
        row = m.blank('s')
        m.apply(row, 'UserPromptSubmit', {}, 100)
        m.apply(row, 'SubagentStart', {'agent_id': 'a1', 'agent_type': 'Explore'}, 101)
        m.apply(row, 'SubagentStart', {'agent_id': 'a2', 'agent_type': 'general-purpose'}, 102)
        m.apply(row, 'PreToolUse', {'agent_id': 'a1', 'tool_name': 'Grep', 'tool_input': {'pattern': 'secret'}}, 103)
        m.apply(row, 'PreToolUse', {'agent_id': 'a2', 'tool_name': 'Bash', 'tool_input': {'command': 'make x'}}, 104)
        label, tip = m.agent_badge(row, 161)
        self.assertEqual(label, '×2')
        self.assertIn('Explore: Reading · Grep · 1m 00s', tip)
        self.assertIn('general-purpose: Working · Bash', tip)
        self.assertNotIn('secret', row['agent_info'])
        m.apply(row, 'PostToolUse', {'agent_id': 'a1', 'tool_name': 'Grep'}, 105)
        self.assertIn('Explore: Thinking', m.agent_badge(row, 106)[1])
        m.apply(row, 'SubagentStop', {'agent_id': 'a1'}, 107)
        m.apply(row, 'SubagentStop', {'agent_id': 'a2'}, 108)
        self.assertIsNone(m.agent_badge(row, 109))
        self.assertEqual(json.loads(row['agent_info']), {})

    def test_remote_rows_move_badge_times_onto_this_clock(self):
        session = dict(m.blank('x'), subagents=json.dumps({'a': 100}), hosts=json.dumps({'h': [100, 'ssh']}))
        row = m.remote_rows('box', [session], 50, True, {}, ['Tifa'])[0]
        self.assertEqual(json.loads(row['subagents']), {'a': 150})
        self.assertEqual(json.loads(row['hosts']), {'h': [150, 'ssh']})


if __name__ == '__main__':
    unittest.main()
