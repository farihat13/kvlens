"""Which cell may a plan check skip? One decision for every check that walks a query registry (check_planir_ldbc,
check_microbench_planir): only a DECLARED skip is a skip; any other exception fails the check with
the cell's name. Declared: a demo-graph query (no param file at any SF), and a capability refusal (the format lacks a
capability the body needs: plan_for and the candidate resolver raise ValueError "<query>: <format> lacks [...]").
"""
import re

DEMO_QUERIES = frozenset({"fof", "friends", "knows"})   # the LDBC registry's demo-graph queries: no param file
# what the demo graph can run (check_planir's round trip): its own three queries and is4, whose reads exist there.
# Every other LDBC query reads edge types or params the demo graph does not have; check_planir_ldbc runs those at sf0.1
DEMO_RUNNABLE = DEMO_QUERIES | {"is4"}
_LACKS = re.compile(r" lacks \[")


def demo_query(q):
    """the skip reason of a query with no param file, or None"""
    return "a demo query: no param file" if q in DEMO_QUERIES else None


def declared_skip(exc):
    """the skip reason when `exc` is a capability refusal, else None (the caller fails the cell)"""
    if isinstance(exc, ValueError) and _LACKS.search(str(exc)):
        return str(exc)[:100]
    return None


def failure(exc):
    """how a swallowed-before exception is reported"""
    return f"{type(exc).__name__}: {str(exc)[:200]}"
