"""Injected stream fixtures are not upstream/identity evidence."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from test_runtime import setup_runtime
from agy_pool.runtime import RequestRouter, Status, Fragment
from agy_pool.scheduler import Scheduler
from agy_pool.state import LeaseStore


class WorkerFixture:
    verified_account = 'primary'

    def __init__(self):
        self.events = iter(())
        self.starts = 0
        self.closes = 0
        self.on_close = lambda: None

    def start(self):
        self.starts += 1

    def read(self):
        return next(self.events)

    def close(self):
        self.closes += 1
        self.on_close()
        return True


class BackendFixture:
    def __init__(self, worker):
        self.worker = worker
        self.context = None

    def prepare(self, context):
        self.context = context
        return self.worker


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite'
        self.store = LeaseStore(self.path, max_workers=1, account_limits={'primary': 1, 'secondary': 1})
        self.addCleanup(self.store.close)
        config, clock, health = setup_runtime()
        scheduler = Scheduler(self.store, queue_timeout_s=3, max_pending=1, clock=clock)
        self.ticket = scheduler.submit(RequestRouter(config, 'interactive/quality', health), session='opaque', owner='owner')
        self.assertEqual(scheduler.poll(), (self.ticket,))
        scheduler.close()
        self.worker = WorkerFixture()
        self.backend = BackendFixture(self.worker)

    def count(self):
        return self.store._db.execute('SELECT COUNT(*) FROM leases').fetchone()[0]

    def session(self):
        from agy_pool.lifecycle import Session
        return Session(self.ticket, self.store, self.backend)

    def test_output_is_committed_before_error_and_never_retried(self):
        from agy_pool import lifecycle
        self.assertTrue(hasattr(lifecycle, 'Output'))
        from agy_pool.lifecycle import Output, BackendError
        from agy_pool.errors import Failure, Kind
        for fragment in Fragment:
            # Each subcase needs its own dispatched request.
            if fragment != Fragment.TEXT:
                self.setUp()
            event = Output(fragment, 'fixture only')
            def events():
                yield event
                raise BackendError(Failure(Kind.UNSUPPORTED_MODEL))
            self.worker.events = events()
            session = self.session().start()
            self.assertIs(next(session), event)
            self.assertEqual(self.count(), 1)
            with self.assertRaises(BackendError):
                next(session)
            self.assertEqual(self.count(), 0)
            self.assertEqual(self.ticket.request.stop_reason, 'visible_output')
            self.assertIsNone(self.ticket.request.next_attempt())
            self.assertEqual(self.worker.starts, 1)

    def test_typed_start_failure_is_finalized_only_after_cleanup(self):
        from agy_pool.lifecycle import BackendError
        from agy_pool.errors import Failure, Kind
        failure = BackendError(Failure(Kind.UNSUPPORTED_MODEL))
        def broken_start():
            raise failure
        def broken_close():
            raise OSError('cleanup failed')
        self.worker.start = broken_start
        self.worker.on_close = broken_close
        session = self.session()
        lease = self.ticket.lease
        with self.assertRaises(OSError) as caught:
            session.start()
        self.assertIs(caught.exception.__context__, failure)
        self.assertIs(self.ticket.lease, lease)
        self.assertEqual(self.ticket.request.status, Status.IN_FLIGHT)
        with self.assertRaises(RuntimeError):
            self.ticket.request.next_attempt()
        self.worker.on_close = lambda: None
        session.close()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.ticket.request.status, Status.READY)
        self.assertEqual(self.worker.starts, 0)

    def test_interruptions_clean_up_and_propagate(self):
        for phase in ('prepare', 'start', 'read'):
            for exception_type in (KeyboardInterrupt, SystemExit):
                with self.subTest(phase=phase, exception=exception_type):
                    if phase != 'prepare' or exception_type is not KeyboardInterrupt:
                        self.setUp()
                    interrupted = exception_type('interrupted')
                    def interrupt(*args, **kwargs):
                        raise interrupted
                    if phase == 'prepare':
                        self.backend.prepare = interrupt
                    else:
                        setattr(self.worker, phase, interrupt)
                    session = self.session()
                    if phase == 'read':
                        session.start()
                    with self.assertRaises(exception_type) as caught:
                        next(session) if phase == 'read' else session.start()
                    self.assertIs(caught.exception, interrupted)
                    self.assertEqual(self.count(), 0)
                    self.assertEqual(self.worker.closes, 0 if phase == 'prepare' else 1)
                    self.assertEqual(self.ticket.request.status, Status.STOPPED)

    def test_prepare_failure_releases_without_worker(self):
        def broken(context):
            raise OSError('prepare failed before resources exist')
        self.backend.prepare = broken
        session = self.session()
        with self.assertRaises(OSError):
            session.start()
        self.assertEqual(self.count(), 0)

    def test_single_owner_and_no_restart(self):
        session = self.session()
        with self.assertRaises(ValueError):
            self.session()
        with self.assertRaises(RuntimeError):
            next(session)
        session.start()
        with self.assertRaises(RuntimeError):
            session.start()
        session.close()
        with self.assertRaises(RuntimeError):
            session.start()
        self.assertEqual(self.worker.starts, 1)

    def test_release_contention_keeps_lease_without_reclosing_worker(self):
        import sqlite3
        session = self.session().start()
        lease = self.ticket.lease
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        self.store._db.execute('PRAGMA busy_timeout=0')
        blocker.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(sqlite3.OperationalError):
                session.close()
            self.assertIs(self.ticket.lease, lease)
            self.assertEqual(self.ticket.status, 'cleanup_pending')
            self.assertEqual(self.ticket.request.status, Status.IN_FLIGHT)
            self.assertEqual(self.count(), 1)
        finally:
            blocker.execute('ROLLBACK')
        session.close()
        self.assertEqual(self.worker.closes, 1)
        self.assertEqual(self.count(), 0)

    def test_release_must_be_confirmed(self):
        session = self.session().start()
        release = self.store.release
        self.store.release = lambda *args, **kwargs: False
        with self.assertRaises(RuntimeError):
            session.close()
        self.assertEqual(self.count(), 1)
        self.assertIsNotNone(self.ticket.lease)
        self.store.release = release
        session.close()
        self.assertEqual(self.count(), 0)

    def test_cancel_waits_for_real_direct_child_before_release(self):
        import subprocess
        import sys
        from unittest.mock import patch
        worker = self.worker
        worker.process = None
        def start():
            worker.process = subprocess.Popen([sys.executable, '-u', '-c',
                "import time; print('ready', flush=True); time.sleep(60)"], stdout=subprocess.PIPE)
            self.assertEqual(worker.process.stdout.readline().strip(), b'ready')
        def close():
            worker.process.terminate()
            worker.process.wait(timeout=10)
            worker.process.stdout.close()
            return worker.process.poll() is not None
        worker.start = start
        worker.close = close
        session = self.session().start()
        def safety_cleanup():
            if worker.process.poll() is None:
                worker.process.kill()
                worker.process.wait(timeout=10)
            worker.process.stdout.close()
        self.addCleanup(safety_cleanup)
        release = self.store.release
        def checked_release(*args, **kwargs):
            self.assertIsNotNone(worker.process.poll())
            self.assertEqual(self.count(), 1)
            return release(*args, **kwargs)
        with patch.object(self.store, 'release', checked_release):
            session.cancel()
        session.cancel()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.ticket.status, 'cancelled')

    def test_eof_cleanup_stopiteration_is_not_silent_and_can_retry(self):
        session = self.session().start()
        lease = self.ticket.lease
        failure = StopIteration('close failed')
        def broken():
            raise failure
        self.worker.on_close = broken
        with self.assertRaises(RuntimeError) as caught:
            list(session)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(self.ticket.status, 'cleanup_pending')
        self.assertEqual(self.ticket.request.status, Status.IN_FLIGHT)
        self.assertIs(self.ticket.lease, lease)
        self.assertEqual(self.count(), 1)
        with self.assertRaises(RuntimeError):
            next(session)
        self.worker.on_close = lambda: None
        session.close()
        self.assertEqual(self.ticket.request.status, Status.SUCCEEDED)
        self.assertEqual(self.count(), 0)
        self.assertEqual(list(session), [])

    def test_eof_contention_retries_cleanup_without_turning_success_into_cancel(self):
        import sqlite3
        session = self.session().start()
        release = self.store.release
        def busy(*args, **kwargs):
            raise sqlite3.OperationalError('locked')
        self.store.release = busy
        with self.assertRaises(sqlite3.OperationalError):
            next(session)
        self.assertEqual(self.count(), 1)
        self.store.release = release
        session.cancel()
        self.assertEqual(self.ticket.request.status, Status.SUCCEEDED)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.worker.closes, 1)

    def test_read_error_cleanup_stopiteration_retains_error_and_retry(self):
        from agy_pool.lifecycle import BackendError
        from agy_pool.errors import Failure, Kind
        read_failure = BackendError(Failure(Kind.UNSUPPORTED_MODEL))
        cleanup_failure = StopIteration('close failed')
        def broken_read():
            raise read_failure
        def broken_close():
            raise cleanup_failure
        self.worker.read = broken_read
        self.worker.on_close = broken_close
        session = self.session().start()
        lease = self.ticket.lease
        with self.assertRaises(RuntimeError) as caught:
            list(session)
        self.assertIs(caught.exception.__cause__, cleanup_failure)
        self.assertIs(cleanup_failure.__context__, read_failure)
        self.assertEqual(self.ticket.status, 'cleanup_pending')
        self.assertEqual(self.ticket.request.status, Status.IN_FLIGHT)
        self.assertIs(self.ticket.lease, lease)
        self.assertEqual(self.count(), 1)
        self.worker.on_close = lambda: None
        session.close()
        self.assertEqual(self.ticket.request.status, Status.READY)
        self.assertEqual(self.count(), 0)

    def test_iteration_cleanup_stopiteration_from_release_and_finalization(self):
        for phase in ('release', 'finalize'):
            with self.subTest(phase=phase):
                if phase == 'finalize':
                    self.setUp()
                session = self.session().start()
                target = self.store if phase == 'release' else self.ticket.request
                name = 'release' if phase == 'release' else 'succeed'
                original = getattr(target, name)
                failure = StopIteration(phase)
                def broken(*args, **kwargs):
                    raise failure
                setattr(target, name, broken)
                with self.assertRaises(RuntimeError) as caught:
                    next(session)
                self.assertIs(caught.exception.__cause__, failure)
                self.assertEqual(self.ticket.status, 'cleanup_pending')
                self.assertEqual(self.ticket.request.status, Status.IN_FLIGHT)
                self.assertEqual(self.count(), 1 if phase == 'release' else 0)
                setattr(target, name, original)
                session.close()
                self.assertEqual(self.ticket.request.status, Status.SUCCEEDED)
                self.assertEqual(self.worker.closes, 1)
                self.assertEqual(self.count(), 0)

    def test_read_interrupt_with_cleanup_stopiteration_preserves_exception_chain(self):
        for exception_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception=exception_type):
                if exception_type is SystemExit:
                    self.setUp()
                interruption = exception_type('interrupted')
                cleanup_failure = StopIteration('close failed')
                def read():
                    raise interruption
                def close():
                    raise cleanup_failure
                self.worker.read = read
                self.worker.on_close = close
                session = self.session().start()
                with self.assertRaises(RuntimeError) as caught:
                    next(session)
                self.assertIs(caught.exception.__cause__, cleanup_failure)
                self.assertIs(cleanup_failure.__context__, interruption)
                self.assertEqual(self.ticket.request.status, Status.IN_FLIGHT)
                self.assertEqual(self.count(), 1)
                self.worker.on_close = lambda: None
                session.close()
                self.assertEqual(self.ticket.request.status, Status.STOPPED)
                self.assertEqual(self.count(), 0)

    def test_stream_error_cleanup_failure_keeps_original_error_and_no_retry(self):
        from agy_pool.lifecycle import Output, BackendError
        from agy_pool.errors import Failure, Kind
        def events():
            yield Output(Fragment.TEXT, 'fixture')
            raise BackendError(Failure(Kind.UNSUPPORTED_MODEL))
        self.worker.events = events()
        session = self.session().start()
        next(session)
        def broken():
            raise OSError('close failed')
        self.worker.on_close = broken
        with self.assertRaises(OSError) as caught:
            next(session)
        self.assertIsInstance(caught.exception.__context__, BackendError)
        with self.assertRaises(RuntimeError):
            self.ticket.request.next_attempt()
        self.worker.on_close = lambda: None
        session.close()
        self.assertEqual(self.ticket.request.stop_reason, 'visible_output')
        self.assertEqual(self.count(), 0)

    def test_generic_read_error_and_invalid_fragment_stop_closed(self):
        from agy_pool.lifecycle import Output
        for event in (OSError('stream failed'), Output('invalid', 'fixture')):
            with self.subTest(event=event):
                if not isinstance(event, OSError):
                    self.setUp()
                def events():
                    if isinstance(event, Exception):
                        raise event
                    yield event
                self.worker.events = events()
                session = self.session().start()
                with self.assertRaises((OSError, ValueError)):
                    next(session)
                self.assertEqual(self.count(), 0)
                self.assertEqual(self.ticket.request.status, Status.STOPPED)

    def test_missing_or_mismatched_identity_is_not_started(self):
        for identity in ('secondary', ''):
            with self.subTest(identity=identity):
                if identity == '':
                    self.setUp()
                self.worker.verified_account = identity
                session = self.session()
                with self.assertRaises(ValueError):
                    session.start()
                self.assertEqual(self.worker.starts, 0)
                self.assertEqual(self.count(), 0)

    def test_finalize_failure_retries_without_releasing_twice(self):
        session = self.session().start()
        succeed = self.ticket.request.succeed
        def broken(attempt):
            raise RuntimeError('injected finalization failure')
        self.ticket.request.succeed = broken
        with self.assertRaises(RuntimeError):
            next(session)
        self.assertEqual(self.count(), 0)
        self.ticket.request.succeed = succeed
        session.close()
        self.assertEqual(self.ticket.request.status, Status.SUCCEEDED)
        self.assertEqual(self.worker.closes, 1)

    def test_preoutput_typed_error_allows_only_explicit_retry_after_release(self):
        from agy_pool.lifecycle import BackendError
        from agy_pool.errors import Failure, Kind
        def events():
            raise BackendError(Failure(Kind.UNSUPPORTED_MODEL))
            yield
        self.worker.events = events()
        session = self.session().start()
        with self.assertRaises(BackendError):
            next(session)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.ticket.request.status, Status.READY)
        self.assertEqual(self.worker.starts, 1)

    def test_start_failure_with_close_failure_can_be_retried(self):
        def broken_start():
            raise ValueError('startup failed')
        def broken_close():
            raise OSError('cleanup failed')
        self.worker.start = broken_start
        self.worker.on_close = broken_close
        session = self.session()
        with self.assertRaises(OSError) as caught:
            session.start()
        self.assertIsInstance(caught.exception.__context__, ValueError)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.ticket.status, 'cleanup_pending')
        self.worker.on_close = lambda: None
        session.close()
        self.assertEqual(self.count(), 0)

    def test_close_before_start_does_not_prepare_or_launch(self):
        session = self.session()
        session.close()
        self.assertIsNone(self.backend.context)
        self.assertEqual(self.worker.starts, 0)
        self.assertEqual(self.count(), 0)

    def test_start_failure_closes_retained_handle(self):
        def broken():
            raise OSError('fixture startup failed')
        self.worker.start = broken
        session = self.session()
        with self.assertRaises(OSError):
            session.start()
        self.assertEqual(self.worker.closes, 1)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.ticket.request.status, Status.STOPPED)

    def test_identity_must_match_before_start(self):
        self.worker.verified_account = None
        session = self.session()
        with self.assertRaises(ValueError):
            session.start()
        self.assertEqual(self.worker.starts, 0)
        self.assertEqual(self.worker.closes, 1)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.ticket.request.status, Status.STOPPED)

    def test_unconfirmed_close_never_releases_or_reads_again(self):
        session = self.session().start()
        self.worker.close = lambda: False
        with self.assertRaises(RuntimeError):
            session.close()
        self.assertEqual(self.count(), 1)
        with self.assertRaises(RuntimeError):
            next(session)
        self.worker.close = lambda: True
        session.close()
        self.assertEqual(self.count(), 0)

    def test_close_failure_retains_ownership_and_repeated_close_retries(self):
        session = self.session().start()
        lease = self.ticket.lease
        def broken():
            raise OSError('fixture close failed')
        self.worker.on_close = broken
        with self.assertRaises(OSError):
            session.close()
        self.assertIs(self.ticket.lease, lease)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.ticket.request.status, Status.IN_FLIGHT)
        self.worker.on_close = lambda: None
        session.close()
        session.close()
        self.assertEqual(self.worker.closes, 2)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.ticket.request.status, Status.STOPPED)
        self.assertEqual(list(session), [])

    def test_eof_closes_worker_before_release(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_pool.lifecycle'))
        session = self.session()
        self.worker.on_close = lambda: self.assertEqual(self.count(), 1)
        session.start()
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.backend.context.session, 'opaque')
        self.assertEqual(list(session), [])
        self.assertEqual(self.worker.closes, 1)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.ticket.request.status, Status.SUCCEEDED)
