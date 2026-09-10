"""The replay harness must still report a task when its first API request fails."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('replay_eval', Path(__file__).parents[1] / 'scripts' / 'run_eval.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class ReplayFailureTests(unittest.TestCase):
    def test_absent_prior_state_is_failure_instead_of_harness_crash(self):
        first = {'state': None}
        current = {'state': None, 'product_ids': [], 'kind': 'execution_limited',
                   'runtime': {'error': 'APIConnectionError'}}
        failures = replay.check_turn(current, {'kind': ['answered'], 'facts_survive': True},
                                     [first, current], [], {'task_ids': []}, {})
        self.assertIn('runtime_failure', failures)
        self.assertIn('prior_state_unavailable', failures)
