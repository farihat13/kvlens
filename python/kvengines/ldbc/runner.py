"""LDBC-specific query runner.

Extends the general-purpose QueryRunner with LDBC conventions: query file
loading, parameter file loading, result parsing into schema dataclasses,
and structured result saving.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kvengines.client import DatabaseClient, QueryResult
from kvengines.ldbc.params import read_param_file
from kvengines.ldbc.parsers import LDBCParser
from kvengines.results import ResultStore
from kvengines.runner import QueryRunner


# Query file extensions per database.
_QUERY_EXTENSIONS: dict[str, str] = {
    "dgraph": ".graphql",
    "nebula": ".gql",
    "neo4j": ".cypher",
    "arangodb": ".aql",
}

# Query file name patterns per query type.
_QUERY_FILE_PATTERNS: dict[str, str] = {
    "ic": "interactive-complex-{n}",
    "is": "interactive-short-{n}",
}

_PARAM_FILE_PATTERNS: dict[str, str] = {
    "ic": "interactive_{n}_param.txt",
    "is": "interactive_short_{n}_param.txt",
}


@dataclass
class LDBCQueryResult:
    """Result of a single LDBC query execution."""

    query_result: QueryResult
    """Raw QueryResult from the runner."""

    normalized: list[Any] | None = None
    """Parsed into LDBC schema dataclasses (if parser provided)."""

    query_type: str = ""
    """'ic' or 'is'."""

    query_num: int = 0
    param_idx: int = 0

    parse_ms: float = 0.0
    """Time spent parsing raw results into LDBC schema dataclasses."""


class LDBCRunner:
    """Run LDBC Interactive queries with parsing and result saving.

    Args:
        client: Database client to execute queries against.
        queries_dir: Directory containing query files.
        params_dir: Directory containing LDBC substitution parameter files.
        parser: Optional parser to normalize raw results into LDBC schemas.
        result_store: Optional result store for persisting results.
        sf: Scale factor string (e.g. "0.3", "1", "10") for result saving.
        query_preprocessor: Optional ``(query_text, qnum) -> query_text``.
        param_preprocessor: Optional ``(params_dict, qnum) -> params_dict``.
    """

    def __init__(
        self,
        client: DatabaseClient,
        queries_dir: Path | str,
        params_dir: Path | str,
        parser: LDBCParser | None = None,
        result_store: ResultStore | None = None,
        sf: str = "",
        query_preprocessor: Callable[[str, int], str] | None = None,
        param_preprocessor: Callable[[dict, int], dict] | None = None,
    ) -> None:
        self.client = client
        self.queries_dir = Path(queries_dir)
        self.params_dir = Path(params_dir)
        self.parser = parser
        self.sf = sf
        self.query_preprocessor = query_preprocessor
        self.param_preprocessor = param_preprocessor

        self._runner = QueryRunner(client=client, result_store=result_store)

    def load_query(self, query_type: str, query_num: int) -> str:
        """Load query text from the queries directory.

        Resolves the file name using the client's ``name`` attribute to pick
        the correct extension (``.graphql``, ``.gql``, etc.).
        """
        ext = _QUERY_EXTENSIONS.get(self.client.name, ".graphql")
        pattern = _QUERY_FILE_PATTERNS.get(query_type, "interactive-complex-{n}")
        filename = pattern.format(n=query_num) + ext

        query_file = self.queries_dir / filename
        if not query_file.exists():
            raise FileNotFoundError(f"Query file not found: {query_file}")
        return query_file.read_text(encoding="utf-8")

    def load_params(self, query_type: str, query_num: int, nparam: int = 0) -> list[dict[str, str]]:
        """Load LDBC substitution parameter sets for a query.

        Args:
            nparam: Number of parameter sets to load. 0 means all.
        """
        pattern = _PARAM_FILE_PATTERNS.get(query_type, "interactive_{n}_param.txt")
        param_file = self.params_dir / pattern.format(n=query_num)
        if not param_file.exists():
            raise FileNotFoundError(f"Parameter file not found: {param_file}")
        all_params = read_param_file(param_file)
        return all_params if nparam == 0 else all_params[:nparam]

    def run_query(
        self,
        query_type: str,
        query_num: int,
        param_idx: int = 1,
    ) -> LDBCQueryResult:
        """Run a single LDBC query with one parameter set.

        Args:
            query_type: "ic" or "is".
            query_num: Query number (1-14 for ic, 1-7 for is).
            param_idx: 1-based index into the parameter file.
        """
        query_text = self.load_query(query_type, query_num)
        if self.query_preprocessor:
            query_text = self.query_preprocessor(query_text, query_num)

        params_list = self.load_params(query_type, query_num, nparam=param_idx)
        if param_idx > len(params_list):
            raise IndexError(
                f"param_idx={param_idx} but only {len(params_list)} params available"
            )
        params = params_list[param_idx - 1]  # 1-based to 0-based

        if self.param_preprocessor:
            params = self.param_preprocessor(params, query_num)

        query_id = f"{query_type}{query_num}"
        save_as = None
        if self._runner.result_store and self.sf:
            save_as = (self.client.name, self.sf, query_id, param_idx)

        qr = self._runner.run(query_text, params, save_as=save_as)

        normalized = None
        parse_ms = 0.0
        if self.parser:
            import time
            t0 = time.perf_counter()
            normalized = self.parser.parse(query_type, query_num, qr.raw)
            parse_ms = (time.perf_counter() - t0) * 1000.0

            # Update saved results with normalized data
            if self._runner.result_store and self.sf:
                result_dir = self._runner.result_store._result_dir(
                    self.client.name, self.sf, query_id, param_idx,
                )
                import json
                from kvengines.results import _serialize
                norm_path = result_dir / "normalized.json"
                norm_path.write_text(
                    json.dumps(_serialize(normalized), indent=2, default=str),
                    encoding="utf-8",
                )

        return LDBCQueryResult(
            query_result=qr,
            normalized=normalized,
            query_type=query_type,
            query_num=query_num,
            param_idx=param_idx,
            parse_ms=parse_ms,
        )

    def run_ic(
        self,
        query_range: range = range(1, 15),
        nparam: int = 1,
    ) -> dict[int, list[LDBCQueryResult]]:
        """Run Interactive Complex queries IC1–IC14 (by default).

        Args:
            query_range: Range of IC query numbers.
            nparam: Number of parameter sets per query.
        """
        return self._run_queries("ic", query_range, nparam)

    def run_is(
        self,
        query_range: range = range(1, 8),
        nparam: int = 1,
    ) -> dict[int, list[LDBCQueryResult]]:
        """Run Interactive Short queries IS1–IS7 (by default).

        Args:
            query_range: Range of IS query numbers.
            nparam: Number of parameter sets per query.
        """
        return self._run_queries("is", query_range, nparam)

    def _run_queries(
        self, query_type: str, query_range: range, nparam: int
    ) -> dict[int, list[LDBCQueryResult]]:
        all_results: dict[int, list[LDBCQueryResult]] = {}

        for qnum in query_range:
            results: list[LDBCQueryResult] = []
            for pidx in range(1, nparam + 1):
                result = self.run_query(query_type, qnum, param_idx=pidx)
                results.append(result)
            all_results[qnum] = results

        return all_results
