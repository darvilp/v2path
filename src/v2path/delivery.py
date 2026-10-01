"""Transport-independent delivery outcomes and opt-in retry policy."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str
    code: str | None = None
    detail: str = ''
    attempts: int = 1
    acknowledgements: tuple[bytes, ...] = ()
    http_response: HTTPResponse | None = None

    @property
    def success(self):
        return self.outcome in {'confirmed', 'sent', 'accepted', 'published', 'completed', 'filtered'}


@dataclass(frozen=True)
class Retry:
    attempts: int = 1
    delay: float = 0

    def __post_init__(self):
        if self.attempts < 1 or self.delay < 0:
            raise ValueError('Retry attempts must be positive and delay nonnegative')


@dataclass(frozen=True)
class RetryPolicy:
    rules: dict[str, Retry] = field(default_factory=dict)

    def for_result(self, result):
        if result.success:
            return Retry()
        http_key = f'http:{result.http_response.status}' if result.http_response else None
        return self.rules.get(result.code, self.rules.get(http_key, self.rules.get(result.outcome, Retry())))
