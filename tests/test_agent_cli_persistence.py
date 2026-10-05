import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'persistence_verifier', Path(__file__).resolve().parents[1]
    / 'tasks/agent-cli-ios-persistence/tests/verify.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class NotesApp:
    def __init__(self, persistent=True, delete_all=False):
        self.notes = {}
        self.text = ''
        self.next_id = 0
        self.persistent = persistent
        self.delete_all = delete_all

    def command(self, name, action, *args):
        data = {'ok': True, 'called': True, 'threw': None}
        if action == 'runtime:type':
            self.text = args[0]
        elif action == 'runtime:tap' and args[0] == 'add-note':
            self.next_id += 1
            self.notes[f'note-{self.next_id}'] = self.text
            self.text = ''
        elif action == 'runtime:tap':
            if self.delete_all:
                self.notes.clear()
            else:
                self.notes.pop(args[0].removeprefix('delete-'))
        elif action == 'runtime:reload':
            if not self.persistent:
                self.notes.clear()
            data = {'reloaded': True}
        elif action == 'runtime:tree':
            data = {'ok': True, 'truncated': False, 'allScreens': False, 'nodes': [
                {'testID': 'note-list', 'text': None}, {'testID': 'note-input', 'text': self.text},
                *[{'testID': key, 'text': value} for key, value in self.notes.items()],
            ]}
        return {'exit_code': 0, 'timed_out': False, 'data': data}


class PersistenceVerifierTest(unittest.TestCase):
    def test_smoke_requires_this_project_local_ios_and_route_verification(self):
        result = {'exit_code': 0, 'data': {'ok': True, 'projectRootMatched': True,
                  'deviceBackend': 'local-ios', 'runtimeSupported': True,
                  'routeCheck': {'checked': True, 'ok': True}}}
        self.assertTrue(verifier.simulator_ok(result))
        result['data']['deviceBackend'] = 'eas'
        self.assertFalse(verifier.simulator_ok(result))
        result['data']['deviceBackend'] = 'local-ios'
        result['data']['routeCheck'] = None
        self.assertFalse(verifier.simulator_ok(result))

    @patch.object(verifier.time, 'sleep')
    def test_persistent_notes_pass(self, _):
        checks, _ = verifier.exercise_notes(NotesApp().command, 'first unique note', 'second unique note')
        self.assertTrue(all(checks.values()))

    @patch.object(verifier.time, 'sleep')
    def test_memory_only_seed_fails_persistence(self, _):
        checks, _ = verifier.exercise_notes(NotesApp(persistent=False).command, 'first', 'second')
        self.assertTrue(checks['created'])
        self.assertFalse(checks['persisted'])
        self.assertFalse(checks['deletion_persisted'])

    @patch.object(verifier.time, 'sleep')
    def test_deleting_everything_fails(self, _):
        checks, _ = verifier.exercise_notes(NotesApp(delete_all=True).command, 'first', 'second')
        self.assertTrue(checks['persisted'])
        self.assertFalse(checks['deleted'])

    def test_echoed_input_and_failed_or_truncated_trees_are_not_notes(self):
        result = {'exit_code': 0, 'data': {'ok': True, 'truncated': False, 'allScreens': False, 'nodes': [
            {'testID': 'note-list'}, {'testID': 'note-input', 'text': 'first'},
        ]}}
        self.assertEqual(verifier.tree_notes(result), {})
        result['data']['truncated'] = True
        self.assertIsNone(verifier.tree_notes(result))
        result['data']['truncated'] = False
        result['exit_code'] = 1
        self.assertIsNone(verifier.tree_notes(result))


if __name__ == '__main__':
    unittest.main()
