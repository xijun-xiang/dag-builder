"""Read-only scheduler-state tests for the bounded advancement controller."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('e3_advance',Path(__file__).resolve().parents[1]/'scripts/e3_advance_expansion.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AdvanceTests(unittest.TestCase):
    def test_requires_all_three_success_states(self):
        with patch.object(module.subprocess,'check_output',return_value='7|COMPLETED|0:0\n7.batch|COMPLETED|0:0\n'):
            self.assertFalse(module.terminal('7'))
        with patch.object(module.subprocess,'check_output',return_value='7|RUNNING|0:0\n7.batch|RUNNING|0:0\n7.0|RUNNING|0:0\n'):
            self.assertFalse(module.terminal('7'))
        with patch.object(module.subprocess,'check_output',return_value='7|COMPLETED|0:0\n7.batch|COMPLETED|0:0\n7.0|COMPLETED|0:0\n'):
            self.assertTrue(module.terminal('7'))

    def test_failures_and_unknown_terminal_states_stop(self):
        for state,code in [('FAILED','1:0'),('TIMEOUT','0:15'),('CANCELLED by 1','0:0'),
                           ('COMPLETED','1:0'),('OUT_OF_MEMORY','0:9'),('NODE_FAIL','0:0')]:
            with self.subTest(state=state),patch.object(module.subprocess,'check_output',return_value=f'7|{state}|{code}\n'):
                with self.assertRaisesRegex(ValueError,'FAILED_JOB_NO_RETRY'):
                    module.terminal('7')
