from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PROBE_PATH = Path(__file__).resolve().parents[1] / '.omo/diagnostics/kakao_frida_plaintext_probe.py'
spec = importlib.util.spec_from_file_location('kakao_reservation_probe_under_test', PROBE_PATH)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)
MARKER = 'synthetic reservation marker 001'
SECRET = 'PRIVATE_TOKEN_NEVER_LOG'
HOOKS = {name: True for name in probe.HOOK_NAMES}


def packet(values=None):
    body = probe._encode_bson_document(values or {'msg': MARKER, 'chatId': 999888777666, 'reserveAt': 1788084000, 'type': 1})
    return struct.pack('<Ih', 42949, 0) + b'RESERVE'.ljust(11, b'\0') + b'\0' + struct.pack('<I', len(body)) + body


def ready(hooks=None):
    return ({'type': 'send', 'payload': {'kind': 'ready', 'hooked': HOOKS if hooks is None else hooks, 'arch': SECRET}}, None)


def candidate(data=None, source='BCryptEncrypt'):
    return ({'type': 'send', 'payload': {'kind': 'plaintext_candidate', 'source': source, 'token': SECRET}}, packet() if data is None else data)


class FakeScript:
    def __init__(self, events=None, fail=None):
        self.events = [ready()] if events is None else events
        self.fail = fail
        self.unloaded = False
        self.exports_sync = SimpleNamespace(get_stats=self.get_stats)

    def on(self, name, callback):
        if self.fail == 'register_callback':
            raise RuntimeError(SECRET)
        self.callback = callback

    def load(self):
        if self.fail == 'load':
            raise RuntimeError(SECRET)
        for event in self.events:
            self.callback(*event)

    def get_stats(self):
        if self.fail == 'stats':
            raise RuntimeError(SECRET)
        return {'bcrypt_calls': 5, 'marker_matches': 1, 'hooked': {**HOOKS, SECRET: SECRET}, 'raw': SECRET, 'send_calls': SECRET}

    def unload(self):
        self.unloaded = True
        if self.fail == 'unload':
            raise RuntimeError(SECRET)


class FakeSession:
    def __init__(self, script, fail=None):
        self.script = script
        self.fail = fail
        self.detached = False

    def create_script(self, source):
        if self.fail == 'create_script':
            raise RuntimeError(SECRET)
        return self.script

    def detach(self):
        self.detached = True
        if self.fail == 'detach':
            raise RuntimeError(SECRET)


class FakeDevice:
    def __init__(self, events=None, fail=None, count=1):
        self.script = FakeScript(events, fail)
        self.session = FakeSession(self.script, fail)
        self.fail = fail
        self.count = count
        self.attached = False

    def enumerate_processes(self):
        if self.fail == 'enumerate':
            raise RuntimeError(SECRET)
        return [SimpleNamespace(name='KakaoTalk.exe', pid=index) for index in range(self.count)]

    def attach(self, pid):
        if self.fail == 'attach':
            raise RuntimeError(SECRET)
        self.attached = True
        return self.session


class ProbeTests(unittest.TestCase):
    def observe(self, device, record=None):
        events = []
        result = probe.run_observation(MARKER, 0.001, device_factory=lambda: device, record=record or events.append)
        return result, events

    def test_short_and_oversize_buffers_do_not_index_past_end(self):
        for size in range(probe.LOCO_HEADER_SIZE):
            with self.subTest(size=size):
                self.assertEqual(probe.find_loco_packets(b'\0' * 6 + b'A' * max(0, size - 6)) if size >= 6 else probe.find_loco_packets(b'A' * size), [])
        self.assertEqual(probe.find_loco_packets(b'A' * (probe.MAX_BUFFER_BYTES + 1)), [])

    def test_redaction_inherits_sensitive_and_unknown_ancestors(self):
        values = {'msg': MARKER, 'chatId': 999888777666, 'reserveAt': 1788084000, 'type': 1,
                  SECRET: {'type': 42, 'status': 'TOKEN_INSIDE_ENUM'},
                  'oauthToken': {'status': 73, 'time': 1234},
                  'unknown': True, 'status': 'PREFIX_' + MARKER, 'time': SECRET}
        result = probe.sanitize_candidate(packet(values), MARKER)
        serialized = json.dumps(result)
        for forbidden in (MARKER, SECRET, '999888777666', 'TOKEN_INSIDE_ENUM', 'oauthToken', 'unknown'):
            self.assertNotIn(forbidden, serialized)
        fields = result[0]['field_paths']
        self.assertIn({'path': 'body.chatId', 'type': 'int64'}, fields)
        self.assertIn({'path': 'body.msg', 'type': 'string'}, fields)
        self.assertIn({'path': 'body.reserveAt', 'type': 'int32', 'value': 1788084000}, fields)
        self.assertNotIn(42, [item.get('value') for item in fields])
        self.assertNotIn(73, [item.get('value') for item in fields])
        self.assertNotIn(1234, [item.get('value') for item in fields])
        self.assertNotIn(True, [item.get('value') for item in fields if item.get('type') == 'boolean'])

    def test_nonfinite_numeric_values_are_omitted(self):
        self.assertIsNone(probe._safe_scalar('time', probe.BsonScalar('double', float('nan')), MARKER))
        self.assertIsNone(probe._safe_scalar('time', probe.BsonScalar('double', float('inf')), MARKER))

    def test_partial_or_different_marker_is_not_a_match(self):
        self.assertEqual(probe.sanitize_candidate(packet({'msg': MARKER + ' suffix'}), MARKER), [])
        self.assertEqual(probe.sanitize_candidate(packet(), 'different marker'), [])
        self.assertEqual(probe.sanitize_candidate(packet()[:-1], MARKER), [])

    def test_validated_unknown_protocol_method_is_retained(self):
        data = bytearray(packet())
        data[6:17] = b'EXAMPLECMD'.ljust(11, b'\0')
        result = probe.sanitize_candidate(bytes(data), MARKER)
        self.assertEqual(result[0]['method'], 'EXAMPLECMD')
        data[6:17] = b'invalidcmd'.ljust(11, b'\0')
        self.assertEqual(probe.sanitize_candidate(bytes(data), MARKER), [])

    def test_marker_is_not_disclosed_as_protocol_method(self):
        result = probe.sanitize_candidate(packet({'msg': 'RESERVE'}), 'RESERVE')
        self.assertEqual(result[0]['method'], 'redacted')

    def test_observation_is_not_reservation_acceptance(self):
        device = FakeDevice([ready(), candidate()])
        result, events = self.observe(device)
        self.assertEqual(result, 0)
        self.assertEqual(events[-1]['state'], 'marker_structure_observed')
        self.assertIsNone(events[-1]['reservation_accepted'])
        self.assertTrue(device.script.unloaded)
        self.assertTrue(device.session.detached)
        serialized = json.dumps(events)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(MARKER, serialized)
        self.assertNotIn('999888777666', serialized)

    def test_callback_error_after_match_is_preserved(self):
        device = FakeDevice([ready(), candidate(), ({'type': 'error', 'description': SECRET}, None)])
        result, events = self.observe(device)
        self.assertEqual(result, 2)
        self.assertFalse(events[-1]['ok'])
        self.assertEqual(events[-1]['error'], 'agent_error')
        self.assertEqual(events[-1]['match_count'], 1)
        self.assertNotIn(SECRET, json.dumps(events))

    def test_parser_callback_exception_is_terminal_failure(self):
        with patch.object(probe, 'sanitize_candidate', side_effect=IndexError(SECRET)):
            result, events = self.observe(FakeDevice([ready(), candidate()]))
        self.assertEqual(result, 2)
        self.assertEqual(events[-1]['error'], 'callback_failed')

    def test_no_hooks_and_only_counter_hooks_are_unavailable(self):
        for hooks in ({}, {'WSASend': True, 'send': True}):
            with self.subTest(hooks=hooks):
                result, events = self.observe(FakeDevice([ready(hooks)]))
                self.assertEqual(result, 3)
                self.assertEqual(events[-1]['state'], 'observation_unavailable')
                self.assertFalse(events[-1]['ok'])

    def test_missing_ready_is_not_no_match(self):
        result, events = self.observe(FakeDevice([]))
        self.assertEqual(events[-1]['state'], 'observation_unavailable')

    def test_timeout_and_unparsed_candidate_are_distinct(self):
        for inputs, state in (([ready()], 'timeout_no_match'), ([ready(), candidate(b'non LOCO')], 'candidate_unparsed')):
            with self.subTest(state=state):
                result, events = self.observe(FakeDevice(inputs))
                self.assertEqual(result, 3)
                self.assertEqual(events[-1]['state'], state)

    def test_startup_and_cleanup_failures_are_safe_and_release_resources(self):
        for fail in ('enumerate', 'attach', 'create_script', 'register_callback', 'load', 'stats', 'unload', 'detach'):
            with self.subTest(fail=fail):
                device = FakeDevice(fail=fail)
                result, events = self.observe(device)
                self.assertEqual(result, 2)
                self.assertEqual(events[-1]['error'], fail + '_failed')
                self.assertNotIn(SECRET, json.dumps(events))
                if device.attached:
                    self.assertTrue(device.session.detached)
                if fail in ('register_callback', 'load', 'stats', 'unload', 'detach'):
                    self.assertTrue(device.script.unloaded)

    def test_device_error_and_no_processes_never_attach(self):
        for count in (0, 2):
            device = FakeDevice(count=count)
            result, events = self.observe(device)
            self.assertEqual(events[-1]['error'], 'kakaotalk_process_count')
            self.assertFalse(device.attached)
        events = []
        result = probe.run_observation(MARKER, 0.001, device_factory=Mock(side_effect=RuntimeError(SECRET)), record=events.append)
        self.assertEqual(events[-1]['error'], 'device_failed')

    def test_interrupted_load_is_cancelled_and_detached(self):
        device = FakeDevice()
        device.script.load = Mock(side_effect=KeyboardInterrupt)
        result, events = self.observe(device)
        self.assertEqual(result, 2)
        self.assertEqual(events[-1]['state'], 'cancelled')
        self.assertFalse(events[-1]['ok'])
        self.assertTrue(device.script.unloaded)
        self.assertTrue(device.session.detached)

    def test_import_error_is_recorded_in_existing_output(self):
        events = []
        result = probe.run_observation(MARKER, 0.001, device_factory=Mock(side_effect=ImportError(SECRET)), record=events.append)
        self.assertEqual(result, 2)
        self.assertEqual(events[0]['state'], 'prepared')
        self.assertEqual(events[-1]['error'], 'frida_not_available')
        self.assertNotIn(SECRET, json.dumps(events))

    def test_persistence_failure_before_start_never_attaches(self):
        factory = Mock()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = probe.run_observation(MARKER, 0.001, device_factory=factory, record=Mock(side_effect=OSError(SECRET)))
        factory.assert_not_called()
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'output_write_failed')
        self.assertNotIn(SECRET, output.getvalue())

    def test_callback_persistence_failure_stops_and_detaches(self):
        device = FakeDevice([ready(), candidate()])
        events = []
        def record(event):
            if event['event'] == 'candidate':
                raise OSError(SECRET)
            events.append(event)
        result, _ = self.observe(device, record=record)
        self.assertEqual(result, 2)
        self.assertEqual(events[-1]['error'], 'output_write_failed')
        self.assertTrue(device.session.detached)

    def test_jsonl_is_exclusive_and_syncs_every_event(self):
        with tempfile.TemporaryDirectory() as temp:
            output_path = Path(temp) / 'probe.jsonl'
            with patch.object(probe.os, 'fsync', wraps=probe.os.fsync) as sync, contextlib.redirect_stdout(io.StringIO()) as stdout:
                result = probe.run_live(MARKER, 0.001, output_path, device_factory=lambda: FakeDevice([ready(), candidate()]))
            self.assertEqual(result, 0)
            events = [json.loads(line) for line in output_path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(sync.call_count, len(events))
            self.assertEqual([e['sequence'] for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(events[-1]['event'], 'terminal')
            self.assertNotIn('field_paths', stdout.getvalue())
            for forbidden in (MARKER, SECRET, '999888777666'):
                self.assertNotIn(forbidden, output_path.read_text(encoding='utf-8') + stdout.getvalue())
            original = output_path.read_bytes()
            factory = Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                result = probe.run_live(MARKER, 0.001, output_path, device_factory=factory)
            self.assertEqual(result, 2)
            factory.assert_not_called()
            self.assertEqual(output_path.read_bytes(), original)

    def test_cli_requires_output_and_finite_timeout(self):
        for args in (['--marker', MARKER], ['--marker', MARKER, '--output', 'x', '--timeout', 'nan'], ['--marker', MARKER, '--output', 'x', '--timeout', '0']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                probe.parse_args(args)
        self.assertTrue(probe.parse_args(['--self-test']).self_test)

    def test_self_test_remains_offline(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe.self_test(), 0)


if __name__ == '__main__':
    unittest.main()
