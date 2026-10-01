"""Public errors and diagnostics. Diagnostics never log or warn automatically."""
from dataclasses import dataclass


class ParseError(ValueError):
    pass


class AssignmentError(ValueError):
    pass


class StaleViewError(RuntimeError):
    pass


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    segment_index: int | None = None
