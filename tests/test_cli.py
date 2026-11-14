import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from test_config import ROOT


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'agy_pool', *args], cwd=ROOT,
                              capture_output=True, text=True)

    def test_validate_and_plan_are_offline_json(self):
        result = self.run_cli('validate', 'examples/pools.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'valid': True, 'mode': 'offline-validation'})
        result = self.run_cli('plan', 'examples/pools.json', 'interactive/quality', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['mode'], 'simulation')
        self.assertEqual(plan['required_capabilities'], ['tools'])
        self.assertEqual(plan['stages'][0]['accounts'], ['primary', 'secondary'])

    def test_cli_errors_fail_closed_without_tracebacks_or_input_echo(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'bad.json'
            path.write_text('{"secret":"DO_NOT_ECHO"}', encoding='utf-8')
            for args in [('validate', str(path)), ('validate', 'missing.json'),
                         ('plan', 'examples/pools.json', 'unknown', '--dry-run'),
                         ('plan', 'examples/pools.json', 'interactive/quality')]:
                with self.subTest(args=args):
                    result = self.run_cli(*args)
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertNotIn('DO_NOT_ECHO', result.stderr)
                    self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
