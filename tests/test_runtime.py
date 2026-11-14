import importlib.util
import json
import unittest
from test_config import example
from test_health import Clock
from agy_pool.config import parse_config
from agy_pool.capabilities import Catalog, ModelSupport
from agy_pool.errors import Failure, Kind, Scope
from agy_pool.health import Health


def setup_runtime(data=None, *, aliases=None, identities=None, support=None):
    data = example() if data is None else data
    config = parse_config(json.dumps(data))
    clock = Clock()
    catalog = Catalog(aliases or {}, identities if identities is not None else config.accounts,
                      support if support is not None else {
                          (a, m): ModelSupport(True, {'tools'}) for a in config.accounts
                          for r in config.routes.values() for m in r.models})
    health = Health(catalog, clock)
    return config, clock, health


class RuntimeTests(unittest.TestCase):
    def test_direct_late_bridge_retains_rollback_until_sqlite_unlock(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from agy_pool.state import LeaseStore
        from agy_pool.runtime import RequestRouter, Status
        config, clock, health = setup_runtime()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            with LeaseStore(path, max_workers=1, account_limits={'primary': 1, 'secondary': 1}) as store:
                blocker = sqlite3.connect(path, isolation_level=None)
                request = RequestRouter(config, 'interactive/quality', health, deadline=101)
                leases = []
                def reserve(model, canonical, accounts):
                    leases.append(store.reserve(accounts, group=request.group, model=canonical, session='s', owner='p'))
                    clock.now = 101
                    blocker.execute('BEGIN IMMEDIATE')
                    return leases[-1].account
                def rollback(account):
                    self.assertEqual(account, leases[-1].account)
                    store.release(leases[-1].token, owner=leases[-1].owner)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        request.next_attempt(reserve=reserve, rollback=rollback)
                    self.assertEqual(request.status, Status.STOPPED)
                    self.assertEqual(request.stop_reason, 'deadline')
                    self.assertEqual(request.visited, frozenset())
                    self.assertTrue(request.cleanup_pending)
                    with self.assertRaises(sqlite3.OperationalError):
                        request.retry_cleanup()
                    self.assertTrue(request.cleanup_pending)
                    self.assertEqual(store._db.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 1)
                    blocker.execute('ROLLBACK')
                    request.retry_cleanup()
                    self.assertFalse(request.cleanup_pending)
                    request.retry_cleanup()
                    self.assertEqual(store._db.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 0)
                    self.assertIsNone(request.next_attempt())
                finally:
                    blocker.close()

    def test_ineligible_reservation_is_rolled_back(self):
        from agy_pool.runtime import RequestRouter
        config, clock, health = setup_runtime()
        request = RequestRouter(config, 'interactive/quality', health)
        released = []
        with self.assertRaises(ValueError):
            request.next_attempt(reserve=lambda *args: 'outside', rollback=released.append)
        self.assertEqual(released, ['outside'])
        self.assertEqual(request.visited, frozenset())

    def test_bridge_requires_rollback_before_invoking_reservation(self):
        from agy_pool.runtime import RequestRouter
        config, clock, health = setup_runtime()
        request = RequestRouter(config, 'interactive/quality', health)
        calls = []
        with self.assertRaises(ValueError):
            request.next_attempt(reserve=lambda *args: calls.append(args))
        self.assertEqual(calls, [])
        self.assertEqual(request.visited, frozenset())

    def test_reservation_bridge_does_not_spend_attempts_on_capacity(self):
        from agy_pool.runtime import RequestRouter, Status
        config, clock, health = setup_runtime()
        request = RequestRouter(config, 'interactive/quality', health)
        calls = []
        def full(model, canonical, accounts):
            calls.append((model, canonical, accounts))
            return None
        self.assertIsNone(request.next_attempt(reserve=full, rollback=lambda account: None))
        self.assertEqual(request.status, Status.READY)
        self.assertEqual(request.visited, frozenset())
        self.assertEqual(calls[0][2], ('primary', 'secondary'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'claude-sonnet-4-6')
        first = request.next_attempt(reserve=lambda m, c, accounts: accounts[-1], rollback=lambda account: None)
        self.assertEqual((first.account, first.number), ('secondary', 1))
        request.fail(first, Failure(Kind.UNSUPPORTED_MODEL))
        self.assertEqual(request.next_attempt(reserve=lambda m, c, accounts: accounts[0], rollback=lambda account: None).account, 'primary')
        other = RequestRouter(config, 'background/fast', health)
        with self.assertRaises(ValueError):
            other.next_attempt(reserve=lambda m, c, accounts: 'outside', rollback=lambda account: None)
        self.assertEqual(other.visited, frozenset())

    def test_routing_boundaries_and_catalog_filtering(self):
        from agy_pool.runtime import RequestRouter
        # Group boundary: background/fast route cannot touch primary account
        config, clock, health = setup_runtime()
        request = RequestRouter(config, 'background/fast', health)
        first = request.next_attempt()
        self.assertEqual(first.account, 'secondary')
        request.fail(first, Failure(Kind.CAPACITY, Scope.MODEL, 10))
        self.assertIsNone(request.next_attempt())
        self.assertEqual(request.stop_reason, 'exhausted')
        self.assertEqual({p[0] for p in request.visited}, {'secondary'})

        # Disabled accounts are blocked
        data = example()
        data['accounts']['secondary']['enabled'] = False
        config, clock, health = setup_runtime(data)
        request = RequestRouter(config, 'interactive/quality', health)
        first = request.next_attempt()
        self.assertEqual(first.account, 'primary')
        request.fail(first, Failure(Kind.CAPACITY, Scope.MODEL, 10))
        self.assertIsNone(request.next_attempt())

        # Unverified accounts blocked
        config, clock, health = setup_runtime(identities={'primary'})
        request = RequestRouter(config, 'interactive/quality', health)
        first = request.next_attempt()
        self.assertEqual(first.account, 'primary')
        request.fail(first, Failure(Kind.CAPACITY, Scope.MODEL, 10))
        self.assertIsNone(request.next_attempt())

        # Missing required capability blocked
        config, clock, health = setup_runtime(support={
            ('primary', 'claude-sonnet-4-6'): ModelSupport(True, frozenset()), # missing 'tools'
            ('secondary', 'claude-sonnet-4-6'): ModelSupport(True, frozenset({'tools'}))
        })
        request = RequestRouter(config, 'interactive/quality', health)
        first = request.next_attempt()
        self.assertEqual(first.account, 'secondary')

        # Canonical model alias health is shared
        aliases = {'claude-sonnet-4-6': 'canonical-claude'}
        config, clock, health = setup_runtime(aliases=aliases, support={
            (a, 'canonical-claude'): ModelSupport(True, frozenset({'tools'})) for a in ('primary', 'secondary')
        })
        health.record('primary', 'canonical-claude', Failure(Kind.QUOTA, Scope.MODEL, 60))
        request = RequestRouter(config, 'interactive/quality', health)
        first = request.next_attempt()
        self.assertEqual(first.account, 'secondary')


    def test_attempt_budget_and_absolute_deadline_bound_transitions(self):
        from agy_pool.runtime import RequestRouter
        data = example()
        data['limits']['max_attempts'] = 1
        config, clock, health = setup_runtime(data)
        request = RequestRouter(config, 'interactive/quality', health)
        request.fail(request.next_attempt(), Failure(Kind.UNSUPPORTED_MODEL))
        self.assertIsNone(request.next_attempt())
        self.assertEqual(request.stop_reason, 'max_attempts')
        config, clock, health = setup_runtime()
        request = RequestRouter(config, 'interactive/quality', health, deadline=101)
        attempt = request.next_attempt()
        clock.now = 101
        request.fail(attempt, Failure(Kind.UNSUPPORTED_MODEL))
        self.assertIsNone(request.next_attempt())
        self.assertEqual(request.stop_reason, 'deadline')
        request = RequestRouter(config, 'interactive/quality', health)
        clock.now += config.limits.request_timeout_s
        self.assertIsNone(request.next_attempt())
        self.assertEqual(request.stop_reason, 'deadline')
        request = RequestRouter(config, 'interactive/quality', health, deadline=clock.now)
        self.assertIsNone(request.next_attempt())
        for bad in (True, -1, float('inf'), float('nan'), '1'):
            with self.assertRaises(ValueError):
                RequestRouter(config, 'interactive/quality', health, deadline=bad)


    def test_terminal_failures_and_visible_fragments_never_replay(self):
        from agy_pool.runtime import RequestRouter, Status
        for kind in (Kind.UNKNOWN, Kind.INVALID_REQUEST, Kind.CONTEXT):
            with self.subTest(kind=kind):
                config, clock, health = setup_runtime()
                request = RequestRouter(config, 'interactive/quality', health)
                attempt = request.next_attempt()
                request.fail(attempt, Failure(kind))
                self.assertEqual(request.status, Status.STOPPED)
                self.assertEqual(request.stop_reason, kind.value)
                self.assertIsNone(request.next_attempt())
        from agy_pool.runtime import Fragment
        for fragment in Fragment:
            with self.subTest(fragment=fragment):
                config, clock, health = setup_runtime()
                request = RequestRouter(config, 'interactive/quality', health)
                attempt = request.next_attempt()
                request.visible(attempt, fragment)
                request.fail(attempt, Failure(Kind.UNSUPPORTED_MODEL))
                self.assertEqual(request.stop_reason, 'visible_output')
                self.assertIsNone(request.next_attempt())
                with self.assertRaises(RuntimeError):
                    request.visible(attempt, fragment)


    def test_same_model_first_then_explicit_fallback_success(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_pool.runtime'))
        from agy_pool.runtime import RequestRouter, Status
        config, clock, health = setup_runtime()
        request = RequestRouter(config, 'interactive/quality', health)
        first = request.next_attempt()
        self.assertEqual((first.account, first.model, first.number), ('primary', 'claude-sonnet-4-6', 1))
        with self.assertRaises(RuntimeError):
            request.next_attempt()
        request.fail(first, Failure(Kind.UNSUPPORTED_MODEL))
        second = request.next_attempt()
        self.assertEqual((second.account, second.model), ('secondary', first.model))
        with self.assertRaises(RuntimeError):
            request.fail(first, Failure())
        request.fail(second, Failure(Kind.UNSUPPORTED_MODEL))
        third = request.next_attempt()
        self.assertEqual((third.account, third.model), ('primary', 'gemini-3.1-pro'))
        request.succeed(third)
        self.assertEqual(request.status, Status.SUCCEEDED)
        self.assertIsNone(request.next_attempt())
        self.assertEqual(len(request.visited), 3)
