import importlib.util
import tempfile
import unittest
from pathlib import Path
from test_runtime import setup_runtime
from agy_pool.runtime import RequestRouter, Status
from agy_pool.state import LeaseStore


class SchedulerTests(unittest.TestCase):
    def test_poll_error_delivers_partial_dispatch_exactly_once(self):
        import sqlite3
        from agy_pool.scheduler import Scheduler
        config, clock, health = setup_runtime()
        with tempfile.TemporaryDirectory() as directory:
            with LeaseStore(Path(directory) / 'state.sqlite', max_workers=2,
                            account_limits={'primary': 1, 'secondary': 1}) as store:
                scheduler = Scheduler(store, queue_timeout_s=3, max_pending=2, clock=clock)
                first = scheduler.submit(RequestRouter(config, 'interactive/quality', health), session='a', owner='a')
                second = scheduler.submit(RequestRouter(config, 'background/fast', health), session='b', owner='b')
                store._db.execute("CREATE TRIGGER reject_b BEFORE INSERT ON leases WHEN NEW.owner='b' BEGIN SELECT RAISE(ABORT, 'B failure'); END")
                try:
                    scheduler.poll()
                except Exception as exc:
                    self.assertEqual(getattr(exc, 'dispatched', None), (first,))
                    self.assertIsInstance(exc.__cause__, sqlite3.IntegrityError)
                else:
                    self.fail('Expected B failure with explicit partial delivery')
                self.assertEqual(first.request.status, Status.IN_FLIGHT)
                self.assertIsNotNone(first.lease)
                store._db.execute('DROP TRIGGER reject_b')
                self.assertEqual(scheduler.poll(), (second,))
                self.assertEqual(scheduler.poll(), ())
                scheduler.close()
                self.assertEqual(store._db.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 2)

    def test_late_cleanup_is_retained_across_locked_lifecycle_paths(self):
        import sqlite3
        from agy_pool.scheduler import Scheduler, PollError
        for retry in ('poll', 'cancel', 'close', 'closed_poll', 'closed_cancel'):
            with self.subTest(retry=retry), tempfile.TemporaryDirectory() as directory:
                config, clock, health = setup_runtime()
                path = Path(directory) / 'state.sqlite'
                blocker = sqlite3.connect(path, isolation_level=None)
                class LateStore(LeaseStore):
                    def reserve(self, *args, **kwargs):
                        lease = super().reserve(*args, **kwargs)
                        clock.now += 3
                        blocker.execute('BEGIN IMMEDIATE')
                        return lease
                try:
                    with LateStore(path, max_workers=1, account_limits={'primary': 1, 'secondary': 1}) as store:
                        scheduler = Scheduler(store, queue_timeout_s=2, max_pending=1, clock=clock)
                        ticket = scheduler.submit(RequestRouter(config, 'interactive/quality', health), session='a', owner='p')
                        with self.assertRaises(PollError):
                            scheduler.poll()
                        self.assertEqual(ticket.status, 'cleanup_pending')
                        self.assertEqual(ticket.request.status, Status.STOPPED)
                        self.assertEqual(scheduler.pending_count, 1)
                        lease = ticket.lease
                        def action():
                            if retry == 'cancel' or retry == 'closed_cancel':
                                return scheduler.cancel(ticket)
                            if retry == 'close':
                                return scheduler.close()
                            return scheduler.poll()
                        if retry.startswith('closed_'):
                            with self.assertRaises(sqlite3.OperationalError):
                                scheduler.close()
                            with self.assertRaises(RuntimeError):
                                scheduler.submit(RequestRouter(config, 'interactive/quality', health), session='b', owner='p')
                        with self.assertRaises((PollError, sqlite3.OperationalError)):
                            action()
                        self.assertEqual(scheduler.pending_count, 1)
                        self.assertIs(ticket.lease, lease)
                        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 1)
                        blocker.execute('ROLLBACK')
                        action()
                        self.assertEqual(scheduler.pending_count, 0)
                        self.assertIsNone(ticket.lease)
                        self.assertEqual(ticket.status, 'expired')
                        self.assertEqual(ticket.request.visited, frozenset())
                        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 0)
                        scheduler.close()
                finally:
                    blocker.close()

    def test_invalid_clock_rejects_submission_without_queueing(self):
        from agy_pool.scheduler import Scheduler
        config, clock, health = setup_runtime()
        with tempfile.TemporaryDirectory() as directory:
            with LeaseStore(Path(directory) / 'state.sqlite', max_workers=1,
                            account_limits={'primary': 1, 'secondary': 1}) as store:
                with Scheduler(store, queue_timeout_s=3, max_pending=1, clock=lambda: -1) as scheduler:
                    with self.assertRaises(ValueError):
                        scheduler.submit(RequestRouter(config, 'interactive/quality', health), session='s', owner='p')
                    self.assertEqual(scheduler.pending_count, 0)

    def test_each_ticket_checks_current_queue_deadline(self):
        from agy_pool.scheduler import Scheduler
        config, clock, health = setup_runtime()
        class AdvancingStore(LeaseStore):
            def reserve(self, *args, **kwargs):
                result = super().reserve(*args, **kwargs)
                clock.now += 3
                return result
        with tempfile.TemporaryDirectory() as directory:
            with AdvancingStore(Path(directory) / 'state.sqlite', max_workers=2,
                                account_limits={'primary': 1, 'secondary': 1}) as store:
                with Scheduler(store, queue_timeout_s=2, max_pending=2, clock=clock) as scheduler:
                    first = scheduler.submit(RequestRouter(config, 'interactive/quality', health), session='a', owner='p')
                    second = scheduler.submit(RequestRouter(config, 'background/fast', health), session='b', owner='p')
                    dispatched = scheduler.poll()
                    self.assertEqual(second.status, 'expired')
                    self.assertEqual(second.request.visited, frozenset())
                    # First reservation completed after its queue deadline too.
                    self.assertEqual(first.status, 'expired')
                    self.assertEqual(dispatched, ())
                    self.assertIsNotNone(store.reserve(('primary',), group='g', model='m', session='x', owner='p'))

    def test_dispatch_bridge_filters_models_groups_and_counts_until_explicit_release(self):
        from agy_pool.scheduler import Scheduler
        config, clock, health = setup_runtime()
        with tempfile.TemporaryDirectory() as directory:
            with LeaseStore(Path(directory) / 'state.sqlite', max_workers=2,
                            account_limits={'primary': 1, 'secondary': 1}) as store:
                with Scheduler(store, queue_timeout_s=3, max_pending=4, clock=clock) as scheduler:
                    first = scheduler.submit(RequestRouter(config, 'background/fast', health), session='s1', owner='p1')
                    second = scheduler.submit(RequestRouter(config, 'interactive/quality', health), session='s2', owner='p2')
                    self.assertEqual(scheduler.poll(), (first, second))
                    self.assertEqual(first.attempt.account, 'secondary')
                    self.assertEqual(second.attempt.account, 'primary')
                    self.assertIn(first.attempt.model, config.routes['background/fast'].models)
                    self.assertEqual(first.lease.account, first.attempt.account)
                    self.assertFalse(scheduler.cancel(first))
                    first.request.succeed(first.attempt)
                    # A completed routing attempt is not confirmation a persistent worker stopped.
                    third = scheduler.submit(RequestRouter(config, 'background/fast', health), session='s3', owner='p3')
                    self.assertEqual(scheduler.poll(), ())
                    self.assertEqual(third.request.visited, frozenset())
                    self.assertTrue(store.set_idle(first.lease.token, owner=first.lease.owner))
                    self.assertEqual(scheduler.poll(), ())
                    self.assertTrue(store.release(first.lease.token, owner=first.lease.owner))
                    self.assertEqual(scheduler.poll(), (third,))
                    queued = scheduler.submit(RequestRouter(config, 'background/fast', health), session='s4', owner='p4')
                self.assertEqual(queued.status, 'cancelled')
                self.assertIsNone(store.reserve(('primary', 'secondary'), group='g', model='m', session='s', owner='p'))
                scheduler.close()
                with self.assertRaises(RuntimeError):
                    scheduler.poll()
                with self.assertRaises(RuntimeError):
                    scheduler.submit(RequestRouter(config, 'background/fast', health), session='s', owner='p')

    def test_invalid_limits_lifecycle_duplicate_requests_and_external_cancel(self):
        from agy_pool.scheduler import Scheduler
        config, clock, health = setup_runtime()
        with tempfile.TemporaryDirectory() as directory:
            with LeaseStore(Path(directory) / 'state.sqlite', max_workers=1,
                            account_limits={'primary': 1, 'secondary': 1}) as store:
                for bad in (True, 0, -1, float('inf'), float('nan'), '1'):
                    with self.assertRaises(ValueError):
                        Scheduler(store, queue_timeout_s=bad, max_pending=1, clock=clock)
                    with self.assertRaises(ValueError):
                        Scheduler(store, queue_timeout_s=1, max_pending=bad, clock=clock)
                scheduler = Scheduler(store, queue_timeout_s=1, max_pending=3, clock=clock)
                request = RequestRouter(config, 'interactive/quality', health)
                with self.assertRaises(ValueError):
                    scheduler.submit(request, session='', owner='p')
                with self.assertRaises(ValueError):
                    scheduler.submit(request, session='s', owner='')
                ticket = scheduler.submit(request, session='s', owner='p')
                with self.assertRaises(ValueError):
                    scheduler.submit(request, session='s', owner='p')
                request.cancel()
                self.assertEqual(scheduler.poll(), ())
                self.assertEqual(ticket.status, 'stopped')
                with self.assertRaises(ValueError):
                    scheduler.submit(request, session='s', owner='p')
                with self.assertRaises(RuntimeError):
                    with scheduler:
                        raise RuntimeError('caller failure')
                self.assertEqual(scheduler.pending_count, 0)

    def test_queue_is_bounded_deadlined_and_cancellable_without_fake_clock_spin(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_pool.scheduler'))
        from agy_pool.scheduler import Scheduler
        config, clock, health = setup_runtime()
        with tempfile.TemporaryDirectory() as directory:
            with LeaseStore(Path(directory) / 'state.sqlite', max_workers=1,
                            account_limits={'primary': 1, 'secondary': 1}) as store:
                blocker = store.reserve(('primary',), group='g', model='m', session='s', owner='existing')
                scheduler = Scheduler(store, queue_timeout_s=3, max_pending=2, clock=clock)
                request = RequestRouter(config, 'interactive/quality', health)
                ticket = scheduler.submit(request, session='one', owner='p')
                other = RequestRouter(config, 'interactive/quality', health, deadline=101)
                short = scheduler.submit(other, session='two', owner='p')
                with self.assertRaises(OverflowError):
                    scheduler.submit(RequestRouter(config, 'interactive/quality', health), session='three', owner='p')
                for _ in range(100):
                    self.assertEqual(scheduler.poll(), ())
                self.assertEqual(request.visited, frozenset())
                self.assertEqual(ticket.status, 'queued')
                clock.now = 101
                scheduler.poll()
                self.assertEqual(short.status, 'expired')
                self.assertEqual(other.stop_reason, 'queue_deadline')
                self.assertTrue(scheduler.cancel(ticket))
                self.assertFalse(scheduler.cancel(ticket))
                self.assertEqual(request.stop_reason, 'cancelled')
                self.assertEqual(scheduler.pending_count, 0)
                final = scheduler.submit(RequestRouter(config, 'interactive/quality', health), session='last', owner='p')
                clock.now = 104
                self.assertEqual(scheduler.poll(), ())
                self.assertEqual(final.status, 'expired')
                self.assertEqual(store.reserve(('secondary',), group='g', model='m', session='x', owner='p'), None)
                self.assertTrue(store.release(blocker.token, owner=blocker.owner))
