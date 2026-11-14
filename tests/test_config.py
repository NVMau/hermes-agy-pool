import dataclasses
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def example():
    return json.loads((ROOT / 'examples/pools.json').read_text(encoding='utf-8'))


class ConfigTests(unittest.TestCase):
    def test_load_example_into_deeply_immutable_typed_models(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_pool'), 'config package missing')
        from agy_pool.config import load_config, Config, Account, Group, Route, Limits
        config = load_config(ROOT / 'examples/pools.json')
        self.assertIsInstance(config, Config)
        self.assertIsInstance(config.limits, Limits)
        self.assertIsInstance(config.accounts['primary'], Account)
        self.assertIsInstance(config.groups['interactive'], Group)
        self.assertIsInstance(config.routes['interactive/quality'], Route)
        self.assertEqual(config.groups['interactive'].accounts, ('primary', 'secondary'))
        with self.assertRaises(TypeError):
            config.accounts['other'] = config.accounts['primary']
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.accounts['primary'].enabled = False

    def test_strict_schema_rejects_invalid_values(self):
        from agy_pool import config as module
        self.assertTrue(hasattr(module, 'parse_config'), 'strict parser missing')
        cases = []
        def change(path, value):
            data = example()
            node = data
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            cases.append(data)
        for path in [(), ('limits',), ('accounts', 'primary'),
                     ('groups', 'interactive'), ('routes', 'interactive/quality')]:
            change((*path, 'unexpected'), 'rejected')
        for value in [True, 0, 2, '1', 1.0, None]:
            change(('schema_version',), value)
        for section in ['accounts', 'groups', 'routes']:
            for value in [{}, [], None]:
                change((section,), value)
            data = example()
            data[section]['bad id'] = next(iter(data[section].values()))
            cases.append(data)
        for field in ['max_workers', 'max_attempts', 'queue_timeout_s', 'request_timeout_s']:
            for value in [True, False, 0, -1, float('inf'), float('nan'), '2', None]:
                change(('limits', field), value)
        change(('limits', 'max_workers'), 1.5)
        change(('limits', 'max_attempts'), 1.5)
        for value in [True, 0, -1, 1.5, '1', None]:
            change(('accounts', 'primary', 'max_concurrency'), value)
        change(('accounts', 'primary', 'enabled'), 1)
        change(('accounts', 'primary', 'backend'), 'shell')
        change(('accounts', 'primary', 'identity_ref'), '../private')
        for value in [[], ['primary', 'primary'], ['missing'], 'primary', [True]]:
            change(('groups', 'interactive', 'accounts'), value)
        change(('groups', 'interactive', 'selection'), 'random')
        change(('routes', 'interactive/quality', 'group'), 'missing')
        for value in [[], ['a', 'a'], ['bad model'], 'model', [1]]:
            change(('routes', 'interactive/quality', 'models'), value)
        for field, bad in [('fallback_on', 'unknown'), ('required_capabilities', 'unknown')]:
            for value in [[bad], ['tools', 'tools'], None, 'tools', [1]]:
                change(('routes', 'interactive/quality', field), value)
        # Every field is required: absence must not grant fallback permission.
        for path in [(), ('limits',), ('accounts', 'primary'),
                     ('groups', 'interactive'), ('routes', 'interactive/quality')]:
            original = example()
            node = original
            for key in path:
                node = node[key]
            for field in node:
                data = example()
                target = data
                for key in path:
                    target = target[key]
                del target[field]
                cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(module.ConfigError):
                    module.parse_config(json.dumps(data))
        for text in ['null', '[]', '{', '{"schema_version":1,"schema_version":1}',
                     json.dumps(example()).replace('"enabled": true', '"enabled": NaN', 1)]:
            with self.subTest(text=text):
                with self.assertRaises(module.ConfigError):
                    module.parse_config(text)
        self.assertEqual(module.parse_config(json.dumps(example())).schema_version, 1)


if __name__ == '__main__':
    unittest.main()
