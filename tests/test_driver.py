"""Synthetic child processes exercise orchestration, not AGY identity."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from test_runtime import setup_runtime
from agy_pool.lifecycle import BackendError, Metadata, Output
from agy_pool.errors import Failure, Kind
from agy_pool.runtime import Fragment, RequestRouter, Status
from agy_pool.scheduler import Scheduler
from agy_pool.state import LeaseStore


class ProcessWorker:
    def __init__(self, account, script):
        self.verified_account = account  # fixture evidence only
        self.script = script
        self.process = None

    def start(self):
        self.process = subprocess.Popen([sys.executable, '-u', '-c', self.script], stdout=subprocess.PIPE)

    def read(self):
        line = self.process.stdout.readline().decode().strip()
        if line == 'usage':
            return Metadata('usage', 1)
        if line == 'text':
            return Output(Fragment.TEXT, 'hello')
        if line == 'error':
            raise BackendError(Failure(Kind.UNSUPPORTED_MODEL))
        if line:
            raise ValueError('unknown fixture event')
        if self.process.wait() != 0:
            raise RuntimeError('child failed')
        raise StopIteration

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=10)
            self.process.stdout.close()
        return True


class ProcessBackend:
    def __init__(self, scripts):
        self.scripts = scripts
        self.workers = []

    def prepare(self, context):
        worker = ProcessWorker(context.attempt.account, self.scripts[len(self.workers)])
        self.workers.append(worker)
        return worker


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LeaseStore(Path(self.tmp.name) / 's.db', max_workers=1, account_limits={'primary': 1, 'secondary': 1})
        self.addCleanup(self.store.close)
        config, clock, health = setup_runtime()
        self.request = RequestRouter(config, 'interactive/quality', health)
        self.scheduler = Scheduler(self.store, queue_timeout_s=3, max_pending=1, clock=clock)
        self.addCleanup(self.scheduler.close)

    def count(self):
        return self.store._db.execute('SELECT COUNT(*) FROM leases').fetchone()[0]

    def test_rejects_other_pending_tickets_without_dispatching_them(self):
        from agy_pool.driver import Driver
        backend = ProcessBackend(["print('text')"])
        driver = Driver(self.scheduler, self.request, backend, session='s', owner='o')
        other = RequestRouter(self.request._config, 'interactive/quality', self.request._health)
        ticket = self.scheduler.submit(other, session='other', owner='other')
        with self.assertRaises(ValueError):
            next(driver)
        self.assertEqual(ticket.status, 'queued')
        self.assertEqual(self.count(), 0)
        self.scheduler.cancel(ticket)
        driver.close()

    def test_prepare_stopiteration_is_not_driver_eof(self):
        from agy_pool.driver import Driver
        failure = StopIteration('prepare')
        backend = ProcessBackend([])
        def prepare(context):
            raise failure
        backend.prepare = prepare
        driver = Driver(self.scheduler, self.request, backend, session='s', owner='o')
        with self.assertRaises(RuntimeError) as caught:
            list(driver)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(self.request.status, Status.STOPPED)
        self.assertEqual(self.count(), 0)

    def test_start_stopiteration_is_not_driver_eof(self):
        from agy_pool.driver import Driver
        backend = ProcessBackend([])
        failure = StopIteration('start')
        def prepare(context):
            worker = ProcessWorker(context.attempt.account, '')
            def start():
                raise failure
            worker.start = start
            backend.workers.append(worker)
            return worker
        backend.prepare = prepare
        driver = Driver(self.scheduler, self.request, backend, session='s', owner='o')
        with self.assertRaises(RuntimeError) as caught:
            list(driver)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(self.request.status, Status.STOPPED)
        self.assertEqual(self.count(), 0)

    def test_partial_dispatch_error_retains_ownership_for_close(self):
        from agy_pool.driver import Driver
        from agy_pool.scheduler import PollError
        backend = ProcessBackend([])
        poll = self.scheduler.poll
        fault = OSError('after dispatch')
        def partial_poll():
            dispatched = poll()
            raise PollError(dispatched) from fault
        self.scheduler.poll = partial_poll
        driver = Driver(self.scheduler, self.request, backend, session='s', owner='o')
        with self.assertRaises(PollError) as caught:
            next(driver)
        self.assertIs(caught.exception.__cause__, fault)
        self.assertIs(driver.active.ticket, caught.exception.dispatched[0])
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.request.status, Status.IN_FLIGHT)
        driver.close()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.request.status, Status.STOPPED)
        self.assertEqual(backend.workers, [])

    def test_start_failure_cleanup_stopiteration_is_not_driver_eof(self):
        from agy_pool.driver import Driver
        backend = ProcessBackend([])
        startup = ValueError('start')
        cleanup = StopIteration('cleanup')
        def prepare(context):
            worker = ProcessWorker(context.attempt.account, '')
            def start():
                raise startup
            def close():
                raise cleanup
            worker.start, worker.close = start, close
            backend.workers.append(worker)
            return worker
        backend.prepare = prepare
        driver = Driver(self.scheduler, self.request, backend, session='s', owner='o')
        with self.assertRaises(RuntimeError) as caught:
            list(driver)
        self.assertIs(caught.exception.__cause__, cleanup)
        self.assertIs(cleanup.__context__, startup)
        self.assertEqual(driver.ticket.status, 'cleanup_pending')
        self.assertEqual(self.request.status, Status.IN_FLIGHT)
        self.assertEqual(self.count(), 1)
        backend.workers[0].close = lambda: True
        driver.close()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.request.status, Status.STOPPED)

    def test_driver_retains_cleanup_ownership_after_start_failure(self):
        from agy_pool.driver import Driver
        backend = ProcessBackend(["print('text')"])
        def prepare(context):
            worker = ProcessWorker(None, "print('text')")
            worker.close = lambda: False
            backend.workers.append(worker)
            return worker
        backend.prepare = prepare
        driver = Driver(self.scheduler, self.request, backend, session='s', owner='o')
        with self.assertRaises(RuntimeError):
            next(driver)
        self.assertEqual(self.count(), 1)
        self.assertIsNone(backend.workers[0].process)
        backend.workers[0].close = lambda: True
        driver.close()
        self.assertEqual(self.count(), 0)

    def test_visible_failure_is_propagated_without_replay(self):
        from agy_pool.driver import Driver
        backend = ProcessBackend(["print('text'); print('error')", "print('text')"])
        driver = Driver(self.scheduler, self.request, backend, session='s', owner='o')
        self.addCleanup(driver.close)
        self.assertEqual(next(driver), Output(Fragment.TEXT, 'hello'))
        with self.assertRaises(BackendError):
            next(driver)
        self.assertEqual(len(backend.workers), 1)
        self.assertEqual(self.request.stop_reason, 'visible_output')
        self.assertEqual(self.count(), 0)

    def test_real_subprocess_metadata_retry_then_visible_success(self):
        from agy_pool.driver import Driver
        backend = ProcessBackend(["print('usage'); print('error')", "print('text')"])
        driver = Driver(self.scheduler, self.request, backend, session='s', owner='o')
        self.addCleanup(driver.close)
        self.assertEqual(list(driver), [Output(Fragment.TEXT, 'hello')])
        self.assertEqual(len(backend.workers), 2)
        self.assertTrue(all(w.process.poll() is not None for w in backend.workers))
        self.assertEqual(self.request.status, Status.SUCCEEDED)
        self.assertEqual(self.count(), 0)
