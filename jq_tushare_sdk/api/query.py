from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldRef:
    table: str
    name: str

    def __eq__(self, value: Any):  # type: ignore[override]
        return FilterExpr(self, "==", value)

    def __ne__(self, value: Any):  # type: ignore[override]
        return FilterExpr(self, "!=", value)

    def __ge__(self, value: Any):
        return FilterExpr(self, ">=", value)

    def __le__(self, value: Any):
        return FilterExpr(self, "<=", value)

    def __gt__(self, value: Any):
        return FilterExpr(self, ">", value)

    def __lt__(self, value: Any):
        return FilterExpr(self, "<", value)

    def in_(self, values):
        return FilterExpr(self, "in", tuple(values))

    def desc(self):
        return Ordering(self, "desc")

    def asc(self):
        return Ordering(self, "asc")


@dataclass(frozen=True)
class FilterExpr:
    field: FieldRef
    operator: str
    value: Any


@dataclass(frozen=True)
class Ordering:
    field: FieldRef
    direction: str


@dataclass(frozen=True)
class Query:
    fields: tuple[FieldRef, ...]
    filters: tuple[FilterExpr, ...] = field(default_factory=tuple)
    ordering: tuple[Ordering, ...] = field(default_factory=tuple)

    def filter(self, *filters: FilterExpr):
        return Query(self.fields, self.filters + tuple(filters), self.ordering)

    def order_by(self, *ordering: Ordering):
        return Query(self.fields, self.filters, self.ordering + tuple(ordering))


def query(*fields: FieldRef) -> Query:
    return Query(tuple(fields))
