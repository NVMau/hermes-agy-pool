import importlib.util
import unittest
from agy_pool.capabilities import Catalog
from agy_pool.errors import Failure, Kind, Scope


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class HealthTests(unittest.TestCase):
    def test_scopes_hints_aliases_and_explicit_auth_reset(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_pool.health'))
        from agy_pool.health import Health
        clock = Clock()
        health = Health(Catalog({'alias': 'm'}, (), {}), clock, cooldown_s=5)
        health.record('a', 'alias', Failure(Kind.QUOTA, Scope.MODEL, 20))
        self.assertFalse(health.available('a', 'm'))
        self.assertTrue(health.available('a', 'other'))
        self.assertTrue(health.available('b', 'm'))
        clock.now += 5
        health.record('a', 'm', Failure(Kind.CAPACITY, Scope.MODEL, 1))
        clock.now += 14
        self.assertFalse(health.available('a', 'alias'))
        clock.now += 1
        self.assertTrue(health.available('a', 'm'))
        health.record('a', 'm', Failure(Kind.QUOTA))
        self.assertFalse(health.available('a', 'other'))  # unknown scope is account-wide
        clock.now += 5
        self.assertTrue(health.available('a', 'other'))
        health.record('a', 'm', Failure(Kind.AUTH, Scope.MODEL, 0))
        clock.now += 10000
        self.assertFalse(health.available('a', 'other'))
        health.reset_auth('a')
        self.assertTrue(health.available('a', 'other'))
        health.record('a', 'm', Failure(Kind.UNKNOWN))
        self.assertTrue(health.available('a', 'm'))
        for invalid in (True, -1, float('nan'), float('inf'), 10 ** 1000):
            with self.assertRaises(ValueError):
                Health(health.catalog, clock, cooldown_s=invalid)
        clock.now = float('nan')
        with self.assertRaises(ValueError):
            health.available('a', 'm')
