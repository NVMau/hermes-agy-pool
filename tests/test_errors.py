import importlib.util
import unittest


class ErrorTests(unittest.TestCase):
    def test_structured_errors_are_strict_and_terminal_by_default(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_pool.errors'))
        from agy_pool.errors import Failure, Kind, Scope
        self.assertFalse(Failure().retryable)
        for kind in (Kind.UNKNOWN, Kind.INVALID_REQUEST, Kind.CONTEXT):
            self.assertFalse(Failure(kind).retryable)
        self.assertTrue(Failure(Kind.QUOTA, Scope.MODEL, 12).retryable)
        for hint in (True, -1, float('nan'), float('inf'), '1'):
            with self.subTest(hint=hint), self.assertRaises(ValueError):
                Failure(Kind.CAPACITY, retry_after_s=hint)
        for args in [('quota',), (Kind.QUOTA, 'account')]:
            with self.assertRaises(ValueError):
                Failure(*args)
