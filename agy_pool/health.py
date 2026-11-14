"""Single-owner in-memory health shared across requests and groups."""
from time import monotonic
from .capabilities import Catalog
from .errors import Failure, Kind, Scope, finite_number


class Health:
    def __init__(self, catalog: Catalog, clock=monotonic, *, cooldown_s=30):
        self.catalog = catalog
        self.clock = clock
        self.cooldown_s = finite_number(cooldown_s)
        self._until = {}
        self._quarantined = set()

    def now(self):
        return finite_number(self.clock())

    def available(self, account: str, model: str) -> bool:
        now = self.now()
        model = self.catalog.canonical(model)
        return (account not in self._quarantined
                and now >= self._until.get((account, None), 0)
                and now >= self._until.get((account, model), 0))

    def record(self, account: str, model: str, failure: Failure):
        if not isinstance(failure, Failure):
            raise ValueError('Expected structured Failure')
        if failure.kind == Kind.AUTH:
            self._quarantined.add(account)
        elif failure.retryable:
            # Unknown quota/capacity scope may cover the whole account.
            scoped = failure.scope == Scope.MODEL or failure.kind == Kind.UNSUPPORTED_MODEL
            key = (account, self.catalog.canonical(model) if scoped else None)
            delay = self.cooldown_s if failure.retry_after_s is None else failure.retry_after_s
            until = finite_number(self.now() + delay)
            self._until[key] = max(self._until.get(key, 0), until)

    def reset_auth(self, account: str):
        """Administrative reset after re-verification; does not erase cooldowns."""
        self._quarantined.discard(account)
