"""BI substitution parameters -- the file conventions and the query -> param-family mapping, mirroring
workloads/microbench/params.py.

Params are GENERATED from the LDBC CSVs by genparams.py (the spec's factor-table idea, 8.3: pick a
percentile of the factor a query's cost follows -- messages per day, per Tag, per TagClass -- so a
"typical" (p50) and a "heavy" (p95 / max) parameter are named, not sampled) and COMMITTED. Deterministic, so a
missing SF regenerates byte-identically.
Files are pipe-delimited with a header, like the LDBC substitution files; every row carries its stratum.
"""
import os
from kvevaluator.datasets import params_in

# params.py -> bi -> workloads -> evaluator -> code -> <experiment>

# family file -> the queries it feeds (genparams.py imports FAMILIES to know what to write)
FAMILIES = ["message_dates", "message_filter", "date_windows", "tagclass_dates", "tags", "tagclass_countries", "country_pairs", "country_windows", "tag_days", "person_country_class", "interest_tags", "propagation_tags", "forum_dates"]

# query -> (family, args builder): one raw row dict -> the plan's args
_P = {
    "bi1": ("message_dates", lambda r: {"datetime": int(r["datetime"])}),
    "bi1_scan": ("message_dates", lambda r: {"datetime": int(r["datetime"])}),      # the twins share the base's params
    "bi12": ("message_filter", lambda r: {"startDate": int(r["startDate"]), "lengthThreshold": int(r["lengthThreshold"]),
                                          "languages": r["languages"].split(";")}),
    "bi12_scan": ("message_filter", lambda r: {"startDate": int(r["startDate"]), "lengthThreshold": int(r["lengthThreshold"]),
                                               "languages": r["languages"].split(";")}),
    "bi12_source": ("message_filter", lambda r: {"startDate": int(r["startDate"]), "lengthThreshold": int(r["lengthThreshold"]),
                                                "languages": r["languages"].split(";")}),
    "bi9": ("date_windows", lambda r: {"startDate": int(r["startDate"]), "endDate": int(r["endDate"])}),
    "bi9_source": ("date_windows", lambda r: {"startDate": int(r["startDate"]), "endDate": int(r["endDate"])}),
    "bi2": ("tagclass_dates", lambda r: {"tagClass": r["tagClass"], "date": int(r["date"])}),
    "bi5": ("tags", lambda r: {"tag": r["tag5"]}),
    "bi6": ("tags", lambda r: {"tag": r["tag5"]}),
    "bi4": ("forum_dates", lambda r: {"date": int(r["date"])}),
    "bi13": ("country_windows", lambda r: {"country": r["country"], "endDate": int(r["endDate"])}),
    "bi18": ("interest_tags", lambda r: {"tag": r["tag"]}),
    "bi10": ("person_country_class", lambda r: {"personId": int(r["personId"]), "country": r["country"], "tagClass": r["tagClass"]}),
    "bi17": ("propagation_tags", lambda r: {"tag": r["tag"], "delta": 12}),     # 12 h: inside the spec's 8-16 h range
    "bi16": ("tag_days", lambda r: {"tagA": r["tagA"], "dateA": int(r["dateA"]), "tagB": r["tagB"], "dateB": int(r["dateB"]),
                                    "maxKnowsLimit": int(r["maxKnowsLimit"])}),
    "bi16_index": ("tag_days", lambda r: {"tagA": r["tagA"], "dateA": int(r["dateA"]), "tagB": r["tagB"], "dateB": int(r["dateB"]),
                                    "maxKnowsLimit": int(r["maxKnowsLimit"])}),
    "bi7": ("tags", lambda r: {"tag": r["tag7"]}),
    "bi8": ("tags", lambda r: {"tag": r["tag8"], "startDate": int(r["startDate"]), "endDate": int(r["endDate"])}),
    "bi3": ("tagclass_countries", lambda r: {"tagClass": r["tagClass"], "country": r["country"]}),
    "bi3_source": ("tagclass_countries", lambda r: {"tagClass": r["tagClass"], "country": r["country"]}),
    "bi14": ("country_pairs", lambda r: {"country1": r["country1"], "country2": r["country2"]}),
    "bi11": ("country_windows", lambda r: {"country": r["country"], "startDate": int(r["startDate"]), "endDate": int(r["endDate"])}),
}

# the label-run twins (queries/__init__.py) share their base's params and slice
_RUN_TWINS = {"bi9_run": "bi9", "bi9_source_run": "bi9", "bi2_run": "bi2", "bi12_scan_run": "bi12", "bi13_run": "bi13"}
_P.update({twin: _P[base] for twin, base in _RUN_TWINS.items()})

# the (typical, heavy) param indexes the sf0.1 slices use, per query (1-based, as every harness counts)
SLICE_PARAMS = {"bi1": (2, 5), "bi1_scan": (2, 5), "bi12": (1, 5), "bi12_scan": (1, 5), "bi12_source": (1, 5), "bi9_source": (2, 5),
                "bi3_source": (1, 5), "bi9": (2, 5), "bi2": (1, 5), "bi5": (2, 5), "bi7": (3, 5), "bi8": (2, 5),   # bi7: p2 has no related tag at sf0.1
                "bi3": (1, 5), "bi14": (3, 1), "bi11": (1, 5), "bi6": (2, 5), "bi4": (4, 1), "bi13": (1, 5), "bi18": (2, 5), "bi16": (3, 1), "bi16_index": (3, 1), "bi17": (2, 5), "bi10": (3, 1)}
SLICE_PARAMS.update({twin: SLICE_PARAMS[base] for twin, base in _RUN_TWINS.items()})     # bi14: typical = p50, heavy = the correlated top pair


def param_path(sf, family):
    return os.path.join(params_in("ldbc", sf, "bi"), f"{family}.txt")


def param_rows(sf, qname):
    """All raw rows for a query's family (dicts keyed by the header). A missing file is regenerated
    (genparams is deterministic)."""
    family, _ = _P[qname]
    path = param_path(sf, family)
    if not os.path.exists(path):
        from kvevaluator.workloads.bi.genparams import generate
        generate(sf, [family])
    with open(path) as f:
        header = f.readline().rstrip("\n").split("|")
        return [dict(zip(header, line.rstrip("\n").split("|"))) for line in f if line.strip()]


def args_from_row(qname, r):
    _, build = _P[qname]
    return build(r)


def query_params(sf, qname, count=None):
    """Arg dicts for the first `count` params of a query (all when count=None) -- the same contract as
    ldbc.params.query_params and microbench's, so every harness treats the workloads identically."""
    rows = param_rows(sf, qname)
    if count:
        rows = rows[:count]
    return [args_from_row(qname, r) for r in rows]
