"""Transport-independent facts; never classify failures by message substring."""
from dataclasses import dataclass
from enum import Enum
import math


def finite_number(value, *, positive=False):
    """Validate public durations/timestamps without accepting bool or overflow."""
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or value < 0 or (positive and value == 0):
        raise ValueError('Expected a finite nonnegative number')
    return value


class Kind(str, Enum):
    UNKNOWN = 'unknown'
    INVALID_REQUEST = 'invalid_request'
    CONTEXT = 'context'
    AUTH = 'auth'
    UNSUPPORTED_MODEL = 'unsupported_model'
    QUOTA = 'quota'
    CAPACITY = 'capacity'


class Scope(str, Enum):
    UNKNOWN = 'unknown'
    ACCOUNT = 'account'
    MODEL = 'model'  # account + canonical model, never all accounts


@dataclass(frozen=True)
class Failure:
    kind: Kind = Kind.UNKNOWN
    scope: Scope = Scope.UNKNOWN
    retry_after_s: float | None = None

    def __post_init__(self):
        if not isinstance(self.kind, Kind) or not isinstance(self.scope, Scope):
            raise ValueError('Expected typed failure kind and scope')
        if self.retry_after_s is not None:
            finite_number(self.retry_after_s)

    @property
    def retryable(self):
        return self.kind in {Kind.AUTH, Kind.UNSUPPORTED_MODEL, Kind.QUOTA, Kind.CAPACITY}
