from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


@runtime_checkable
class SearchProvider(Protocol):
    def search(self, query: str) -> list[SearchResult]: ...


class StaticSearchProvider:
    """Deterministic search provider useful for fixtures and local runs."""

    def __init__(self, results_by_query: Mapping[str, list[SearchResult]]) -> None:
        self._results_by_query = {query: list(results) for query, results in results_by_query.items()}

    def search(self, query: str) -> list[SearchResult]:
        return list(self._results_by_query.get(query, []))


class CallableSearchProvider:
    """Adapter for host-provided search functions without imposing an SDK."""

    def __init__(self, search_fn: Callable[[str], list[SearchResult]]) -> None:
        self._search_fn = search_fn

    def search(self, query: str) -> list[SearchResult]:
        return list(self._search_fn(query))
