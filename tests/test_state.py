import importlib.util
import tempfile
import multiprocessing
import time
import sqlite3
import unittest
from pathlib import Path


def compete(path, start, output, account, group):
    from agy_pool.state import LeaseStore
    # Each spawned client owns a fresh connection; no inherited SQLite handles.
    with LeaseStore(path, max_workers=2, account_limits={'a': 1, 'b': 2}) as store:
        if not start.wait(10):
            raise RuntimeError('Start gate timed out')
        limit = time.monotonic() + 2
        lease = None
        while lease is None and time.monotonic() < limit:
            lease = store.reserve((account,), group=group, model='m', session=group, owner=group)
            if lease is None:
                time.sleep(.01)
        output.put((account, None if lease is None else lease.token))
        # Intentionally exit without releasing: an exited/unknown owner stays reserved.


class StateTests(unittest.TestCase):
    def test_spawn_clients_cannot_exceed_shared_account_or_global_caps(self):
        from agy_pool.state import LeaseStore
        context = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'state.sqlite')
            with LeaseStore(path, max_workers=2, account_limits={'a': 1, 'b': 2}):
                pass
            start, output = context.Event(), context.Queue()
            processes = [context.Process(target=compete, args=(path, start, output, a, 'g'+str(i)))
                         for i, a in enumerate(('a', 'a', 'a', 'b', 'b', 'b'))]
            started = []
            try:
                for process in processes:
                    process.start()
                    started.append(process)
                start.set()
                results = [output.get(timeout=15) for _ in processes]
                for process in processes:
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
                winners = [a for a, token in results if token is not None]
                self.assertEqual(len(winners), 2)
                self.assertLessEqual(winners.count('a'), 1)
                with LeaseStore(path, max_workers=2, account_limits={'a': 1, 'b': 2}) as store:
                    self.assertIsNone(store.reserve(('a', 'b'), group='after', model='m', session='s', owner='new'))
            finally:
                for process in started:
                    if process.is_alive():
                        process.terminate()
                    process.join(10)
                    process.close()
                output.close()
                output.join_thread()

    def test_spawn_shared_account_limit_is_stricter_than_global_limit(self):
        from agy_pool.state import LeaseStore
        context = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'state.sqlite')
            with LeaseStore(path, max_workers=2, account_limits={'a': 1, 'b': 2}):
                pass
            start, output = context.Event(), context.Queue()
            processes = [context.Process(target=compete, args=(path, start, output, 'a', 'group'+str(i)))
                         for i in range(4)]
            started = []
            try:
                for process in processes:
                    process.start()
                    started.append(process)
                start.set()
                results = [output.get(timeout=15) for _ in processes]
                for process in processes:
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
                self.assertEqual(sum(token is not None for _, token in results), 1)
                with LeaseStore(path, max_workers=2, account_limits={'a': 1, 'b': 2}) as store:
                    self.assertIsNone(store.reserve(('a',), group='after', model='different', session='s', owner='p'))
                    # Global capacity is still available, but only to a different account.
                    self.assertIsNotNone(store.reserve(('b',), group='after', model='different', session='s', owner='p'))
            finally:
                for process in started:
                    if process.is_alive():
                        process.terminate()
                    process.join(10)
                    process.close()
                output.close()
                output.join_thread()

    def test_locked_database_is_nonblocking_and_failed_transaction_rolls_back(self):
        from agy_pool.state import LeaseStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            with LeaseStore(path, max_workers=1, account_limits={'a': 1}) as store:
                blocker = sqlite3.connect(path, isolation_level=None)
                try:
                    blocker.execute('BEGIN IMMEDIATE')
                    before = time.monotonic()
                    self.assertIsNone(store.reserve(('a',), group='g', model='m', session='s', owner='p'))
                    self.assertLess(time.monotonic() - before, 1)
                    blocker.execute('ROLLBACK')
                    blocker.execute("CREATE TRIGGER fail_affinity BEFORE INSERT ON affinity BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
                    with self.assertRaises(sqlite3.IntegrityError):
                        store.reserve(('a',), group='g', model='m', session='s', owner='p')
                    self.assertEqual(blocker.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 0)
                    blocker.execute('DROP TRIGGER fail_affinity')
                    self.assertIsNotNone(store.reserve(('a',), group='g', model='m', session='s', owner='p'))
                finally:
                    blocker.close()

    def test_sticky_affinity_and_round_robin_are_persistent_and_eligible_only(self):
        from agy_pool.state import LeaseStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            def reserve(store, session, accounts=('a', 'b'), model='m', group='g'):
                return store.reserve(accounts, group=group, model=model, session=session, owner='p')
            with LeaseStore(path, max_workers=3, account_limits={'a': 2, 'b': 2}) as store:
                first = reserve(store, 'one')
                self.assertEqual(first.account, 'a')
                store.release(first.token, owner=first.owner)
                sticky = reserve(store, 'one')
                self.assertEqual(sticky.account, 'a')
                store.release(sticky.token, owner=sticky.owner)
            with LeaseStore(path, max_workers=3, account_limits={'a': 2, 'b': 2}) as store:
                second = reserve(store, 'two')
                self.assertEqual(second.account, 'b')
                store.release(second.token, owner=second.owner)
                # Affinity never bypasses the caller's eligible set.
                filtered = reserve(store, 'one', ('b',))
                self.assertEqual(filtered.account, 'b')
                store.release(filtered.token, owner=filtered.owner)
                third = reserve(store, 'three')
                self.assertEqual(third.account, 'a')

    def test_limits_are_validated_shared_and_connections_close(self):
        from agy_pool.state import LeaseStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            for bad in (True, 0, -1, 1.5, float('inf'), '1', 2**63):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        LeaseStore(path, max_workers=bad, account_limits={'a': 1})
                    with self.assertRaises(ValueError):
                        LeaseStore(path, max_workers=1, account_limits={'a': bad})
            with self.assertRaises(ValueError):
                LeaseStore(path, max_workers=1, account_limits={})
            with LeaseStore(path, max_workers=1, account_limits={'a': 1}) as store:
                with self.assertRaises(ValueError):
                    LeaseStore(path, max_workers=2, account_limits={'a': 1})
                with self.assertRaises(ValueError):
                    LeaseStore(path, max_workers=1, account_limits={'a': 2})
                with self.assertRaises(ValueError):
                    store.reserve(('unknown',), group='g', model='m', session='s', owner='p')
                for field in ('group', 'model', 'session', 'owner'):
                    args = dict(group='g', model='m', session='s', owner='p')
                    args[field] = ''
                    with self.assertRaises(ValueError):
                        store.reserve(('a',), **args)
            store.close()
            with self.assertRaises(RuntimeError):
                store.reserve(('a',), group='g', model='m', session='s', owner='p')
            with self.assertRaises(RuntimeError):
                store.__enter__()

    def test_owned_release_is_idempotent_and_idle_unknown_owners_keep_capacity(self):
        from agy_pool.state import LeaseStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            with LeaseStore(path, max_workers=1, account_limits={'a': 1}) as store:
                lease = store.reserve(('a',), group='g', model='m', session='s', owner='unknown-process-start')
                self.assertFalse(store.release(lease.token, owner='wrong-owner'))
                self.assertFalse(store.release('fabricated-token', owner=lease.owner))
                self.assertTrue(store.set_idle(lease.token, owner=lease.owner))
                self.assertIsNone(store.reserve(('a',), group='g', model='m', session='t', owner='new'))
            # Closing the connection or losing a process does not prove termination.
            with LeaseStore(path, max_workers=1, account_limits={'a': 1}) as store:
                self.assertIsNone(store.reserve(('a',), group='g', model='m', session='u', owner='new'))
                self.assertTrue(store.release(lease.token, owner=lease.owner))
                self.assertFalse(store.release(lease.token, owner=lease.owner))
                replacement = store.reserve(('a',), group='g', model='m', session='u', owner='new')
                self.assertNotEqual(replacement.token, lease.token)
                self.assertFalse(store.release(lease.token, owner=lease.owner))
                self.assertIsNone(store.reserve(('a',), group='g', model='m', session='v', owner='new'))

    def test_atomic_shared_account_and_global_reservation_caps(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_pool.state'))
        from agy_pool.state import LeaseStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            with LeaseStore(path, max_workers=2, account_limits={'a': 1, 'b': 2}) as store:
                first = store.reserve(('a',), group='g1', model='m', session='s1', owner='p1')
                self.assertEqual(first.account, 'a')
                with LeaseStore(path, max_workers=2, account_limits={'a': 1, 'b': 2}) as other:
                    self.assertIsNone(other.reserve(('a',), group='g2', model='m2', session='s2', owner='p2'))
                    self.assertIsNotNone(other.reserve(('b',), group='g2', model='m', session='s3', owner='p2'))
                    self.assertIsNone(store.reserve(('b',), group='g1', model='m', session='s4', owner='p1'))
