from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class RuntimeState:
    data_portal: Any
    scheduler: Any = None
    broker: Any = None
    context: Any = None
    g: SimpleNamespace = field(default_factory=SimpleNamespace)
    log: Any = None
    records: list[dict] = field(default_factory=list)
    benchmark: str | None = None
