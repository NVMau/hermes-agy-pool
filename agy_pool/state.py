"""Metadata-only SQLite reservations. No process launch or identity discovery."""
from contextlib import contextmanager
from dataclasses import dataclass
import secrets
import sqlite3
import json
from types import MappingProxyType


@dataclass(frozen=True)
class Lease:
    token: str
    account: str
    owner: str


class LeaseStore:
    def __init__(self, path, *, max_workers, account_limits):
        limits = dict(account_limits)
        for value in (max_workers, *limits.values()):
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError('Expected positive SQLite-range integer limit')
        if not limits or any(not isinstance(k, str) or not k for k in limits):
            raise ValueError('Expected nonempty account limits')
        self._max_workers = max_workers
        self._account_limits = MappingProxyType(limits)
        self._db = sqlite3.connect(path, timeout=5, isolation_level=None)
        try:
            with self._transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
                value = json.dumps([max_workers, limits], sort_keys=True)
                row = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
                if row is not None and row[0] != value:
                    raise ValueError('Shared store limits must match; use a new store for reconfiguration')
                db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (value,))
                db.execute('CREATE TABLE IF NOT EXISTS leases '
                                 '(token TEXT PRIMARY KEY, account TEXT NOT NULL, owner TEXT NOT NULL, idle INTEGER NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS affinity '
                                 '(group_id TEXT, model TEXT, session TEXT, account TEXT NOT NULL, '
                                 'PRIMARY KEY (group_id, model, session))')
                db.execute('CREATE TABLE IF NOT EXISTS cursors '
                                 '(group_id TEXT, model TEXT, account TEXT NOT NULL, PRIMARY KEY (group_id, model))')
            self._db.execute('PRAGMA busy_timeout=0')
        except BaseException:
            self.close()
            raise

    @property
    def max_workers(self):
        return self._max_workers

    @property
    def account_limits(self):
        return self._account_limits

    @contextmanager
    def _transaction(self):
        db = self._db
        if db is None:
            raise RuntimeError('LeaseStore is closed')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise

    def reserve(self, accounts, *, group, model, session, owner):
        accounts = tuple(accounts)
        if any(a not in self.account_limits for a in accounts):
            raise ValueError('Unknown account')
        if any(not isinstance(v, str) or not v for v in (group, model, session, owner)):
            raise ValueError('Expected nonempty metadata identifiers')
        try:
            with self._transaction() as db:
                if db.execute('SELECT COUNT(*) FROM leases').fetchone()[0] >= self.max_workers:
                    return None
                bound = db.execute('SELECT account FROM affinity WHERE group_id=? AND model=? AND session=?',
                                         (group, model, session)).fetchone()
                cursor = db.execute('SELECT account FROM cursors WHERE group_id=? AND model=?',
                                          (group, model)).fetchone()
                ordered = accounts
                if cursor and cursor[0] in accounts:
                    index = accounts.index(cursor[0]) + 1
                    ordered = accounts[index:] + accounts[:index]
                sticky = bound[0] if bound and bound[0] in accounts else None
                if sticky is not None:
                    ordered = (sticky,) + tuple(a for a in ordered if a != sticky)
                for account in ordered:
                    count = db.execute('SELECT COUNT(*) FROM leases WHERE account=?', (account,)).fetchone()[0]
                    if count < self.account_limits[account]:
                        token = secrets.token_urlsafe(32)
                        db.execute('INSERT INTO leases VALUES (?, ?, ?, 0)', (token, account, owner))
                        db.execute('INSERT OR REPLACE INTO affinity VALUES (?, ?, ?, ?)',
                                         (group, model, session, account))
                        if account != sticky:
                            db.execute('INSERT OR REPLACE INTO cursors VALUES (?, ?, ?)',
                                             (group, model, account))
                        return Lease(token, account, owner)
                return None
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return None
            raise

    def release(self, token, *, owner):
        """Bearer token and owner must match; caller must first terminate its worker."""
        with self._transaction() as db:
            return db.execute('DELETE FROM leases WHERE token=? AND owner=?',
                                    (token, owner)).rowcount == 1

    def set_idle(self, token, *, owner):
        with self._transaction() as db:
            return db.execute('UPDATE leases SET idle=1 WHERE token=? AND owner=?',
                                    (token, owner)).rowcount == 1

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        if self._db is None:
            raise RuntimeError('LeaseStore is closed')
        return self

    def __exit__(self, *_):
        self.close()
