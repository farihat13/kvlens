"""Load LDBC query text and parameters, and apply per-DB param preprocessing.

Thin wrappers over kvengines's `read_param_file` + param preprocessors and the repo's
query-file layout. `qtype` is "ic" (interactive-complex) or "is" (interactive-short).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from kvevaluator.paths import ROOT as _ROOT, ldbc_params_dir
from kvevaluator.datasets import params_in

from kvengines.ldbc.params import (
    read_param_file,
    dgraph_param_preprocessor,
    nebula_param_preprocessor,
    arangodb_param_preprocessor,
    tugraph_param_preprocessor,
)

# Active scale factor for the param files. Prefer COSTEVAL_SF (the cost_eval-specific env var)
# but fall back to SF (the bash variable from .env), so the script does the right thing whether
# one, the other, or both may be exported.
SF = os.environ.get("COSTEVAL_SF") or os.environ.get("SF", "0.3")

# Query text is pinned into the experiment (cost_eval/queries/) for reproducibility — the eval runs
# exactly these, independent of edits to the live dgraph/ & nebulagraph/ query dirs. To refresh, re-copy
# from dgraph/queries/*.graphql and nebulagraph/queries/*.gql.
# This repo ships ONE query copy (engines/queries/ldbc/<db>/, the corrected texts); cost_eval's older pinned texts
# are in the source repository (tag artifact-freeze-20261026).
_QUERY_DIR = {"dgraph": _ROOT / "engines" / "queries" / "ldbc" / "dgraph",
              "nebula": _ROOT / "engines" / "queries" / "ldbc" / "nebulagraph",
              "arangodb": _ROOT / "engines" / "queries" / "ldbc" / "arangodb"}
_QUERY_EXT = {"dgraph": "graphql", "nebula": "gql", "arangodb": "aql"}
_QUERY_KIND = {"ic": "interactive-complex", "is": "interactive-short"}

# IS4-7: cost_eval's committed anchors, inputs/params/ldbc/sf<SF>/interactive/short/ (IS5 and IS6 reuse IS4's rows); IC1-12 and
# IS1-3: LDBC's own substitution parameters in the data dir (kvevaluator.paths.ldbc_params_dir).
_PARAM_DIR = params_in("ldbc", SF, "interactive") / "short"                     # IS4-7 (committed)
_LDBC_PARAM_DIR = ldbc_params_dir(SF)                                           # IC1-12, IS1-3 (LDBC's own)


def parse_query_spec(spec: str) -> tuple[str, int]:
    """Parse a compact query spec ('is1', 'ic12') -> (qtype, num) == ('is', 1) / ('ic', 12).
    The shared selector form for the kvcost / accuracy CLIs."""
    qt = "is" if spec.lower().startswith("is") else "ic"
    return qt, int(spec[len(qt):])


def load_query(qtype: str, num: int, db: str) -> str:
    """Read the query file for (qtype, num) on `db` ("dgraph" | "nebula")."""
    path = _QUERY_DIR[db] / f"{_QUERY_KIND[qtype]}-{num}.{_QUERY_EXT[db]}"
    return path.read_text()


def _param_path(qtype: str, num: int) -> Path:
    """Resolve the param file — every query maps to one file in the single cost_eval param dir:
      - IC1-12  -> interactive_<N>_param.txt           (LDBC's hosted file)
      - IS1/2/3 -> interactive_1_param.txt             (person-anchored; reuse IC1's personId column)
      - IS4-7   -> interactive_short_param_<N>.txt      (committed: _4 file + _5/6/7 symlinks)
    SF0.3 has no LDBC `interactive_short_*` files, so IS4-7's are generated (inputs/params/ldbc/README.md)."""
    if qtype == "ic":
        return _LDBC_PARAM_DIR / f"interactive_{num}_param.txt"
    if qtype == "is" and num in (1, 2, 3):
        return _LDBC_PARAM_DIR / "interactive_1_param.txt"            # personId source (shared with IC1)
    if qtype == "is" and num in (4, 5, 6, 7):
        return _PARAM_DIR / f"interactive_short_param_{num}.txt"
    raise NotImplementedError(f"no param file mapping for {qtype}{num}")


def load_params(qtype: str, num: int, nparam: int | None = None) -> list[dict[str, str]]:
    """Read the raw param rows for (qtype, num). `nparam` caps to the first N rows."""
    rows = read_param_file(_param_path(qtype, num))
    return rows[:nparam] if nparam else rows


def prep_params(raw: dict[str, str], db: str, num: int) -> dict:
    """Apply the per-DB param preprocessor (Dgraph: raw ids/ISO dates; Nebula: prefixed ids/ints;
    Arango: int ids/epoch-ms dates, no prefixing; TuGraph: int ids/epoch-ms dates and endDate, no prefixing)."""
    if db == "dgraph":
        return dgraph_param_preprocessor(raw, num)
    if db == "arangodb":
        return arangodb_param_preprocessor(raw, num)
    if db == "tugraph":
        return tugraph_param_preprocessor(raw, num)
    return nebula_param_preprocessor(raw, num)


def filter_params(params: dict, query_text: str, sigil: str = "$") -> dict:
    """Keep only params the query actually references. Lets us read a wider param file (e.g. IC1's
    personId|firstName) and feed IS3, which only declares one. The bind sigil is `$` for Dgraph/Nebula
    and `@` for ArangoDB (AQL `@paramName`)."""
    return {k: v for k, v in params.items() if f"{sigil}{k}" in query_text}


def build_db_params(raw: dict, db: str, qtype: str, num: int, query_text: str) -> dict:
    """Per-DB execution params, filtered to what the query references. IS4-7 carry a bare-int
    `messageId` (plus `kind`/`depth` provenance columns, dropped by the filter); the per-DB
    preprocessor handles the id like any other — Nebula prefixes it to `msg_<id>`, Dgraph keeps the
    int, Arango coerces to int — so IS4-7 need no special case."""
    sigil = "@" if db == "arangodb" else "$"
    return filter_params(prep_params(raw, db, num), query_text, sigil)
