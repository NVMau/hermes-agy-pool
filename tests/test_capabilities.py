import importlib.util
import unittest


class CatalogTests(unittest.TestCase):
    def test_verified_snapshot_resolves_aliases_without_inventing_support(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_pool.capabilities'))
        from agy_pool.capabilities import Catalog, ModelSupport
        aliases = {'alias': 'canonical'}
        identities = {'a'}
        caps = {'tools'}
        entries = {('a', 'canonical'): ModelSupport(True, caps),
                   ('b', 'canonical'): ModelSupport(True, {'tools'}),
                   ('a', 'unknown'): ModelSupport(False, {'tools'})}
        catalog = Catalog(aliases, identities, entries)
        aliases.clear()
        identities.clear()
        caps.clear()
        entries.clear()
        self.assertEqual(catalog.canonical('alias'), 'canonical')
        self.assertTrue(catalog.eligible('a', 'alias', ('tools',)))
        for account, model, required in [('b', 'alias', ()), ('a', 'missing', ()),
                                          ('a', 'unknown', ()), ('a', 'alias', ('vision',))]:
            self.assertFalse(catalog.eligible(account, model, required))
        with self.assertRaises(TypeError):
            catalog.aliases['x'] = 'y'
        for aliases in [{'a': 'b', 'b': 'c'}, {'a': 'a'}, {'a': ''}]:
            with self.assertRaises(ValueError):
                Catalog(aliases, (), {})
        with self.assertRaises(ValueError):
            ModelSupport(1, ())
        with self.assertRaises(ValueError):
            ModelSupport(True, ('magic',))
        with self.assertRaises(ValueError):
            Catalog({'alias': 'canonical'}, ('a',), {('a', 'alias'): ModelSupport(True, ())})
