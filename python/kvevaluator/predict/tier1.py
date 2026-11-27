"""Tier-1 statistics: the full statistics file (build_stats) with every tier-2 field removed, then the flat tier-2 rows a
user with only counts would write, following predict's refusals (inputs/contract/stats.schema.json, x-tier and x-missing).
Derived from the full file alone: no pass over the records. Written beside it as stats-tier1.json, again on every call
(a JSON transform of a second or so: a change to these rules is never missed); the anchor statistics stay one file
beside both (anchor_stats.json).

The flat rows:
  degree_hist            per owners row (E, dir, L): every owner at the mean degree m = edges / owners:
                         [[0, vertices - owners, 0, 0], [bucket of m, owners, edges, edges * m]]
  neighbor_degree_ratio  E|both for every edge type: 1 (no owners row covers 'both'; out and in read the rows above)
  date_days              per dated label and property: an even spread, u / days per day, from the earliest to the latest
                         date (only the first day and the number of days are read from the full file), u from
                         sizes.content prop|L|p
  date_axis              the dataset's declaration, copied from the full file
  burstiness             1 for every key the full file has
  int_values             an even spread over value_range, one count per value, each the holders / the values (holders:
                         sizes.content's u for a vertex property, the edge type's edges for an edge property)
  degree_by_far_label    per declared key O|E|dir|F (the full file's keys: every declared pair of ends, never its
                         values): the owners row's edges split over the far labels F that O|E|dir declares, in
                         proportion to each one's type_counts; each at degree_hist's flat mean m = edges / owners:
                         [n_F, n_F * m] (no owners row: [0, 0])
The tier-1 fields kept as they are, but one cut:
  top_values             per indexed property: the k most common values of the full file's list, k the contract's
                         top_values x-tier1-k (10); its holders kept
  top_degrees            kept whole: both tiers hold the same 10 values per key
The tier-2 fields with no flat form stay out (provenance.withheld names them); a plan that reads one is refused by name.

  python3 -m kvevaluator.predict.build_stats --sf 0.1 --tier 1     # the usual entry (kvcost predict --tier 1 too)
"""
import hashlib
import json
import math
import os

from kvevaluator.paths import CONTRACT_DIR

CONTRACT = CONTRACT_DIR / "stats.schema.json"
FILE_NAME = "stats-tier1.json"
DEFAULT_TIER = 1   # the statistics a prediction reads unless told otherwise: 1 this file, 2 the full one
FLAT = ("burstiness", "date_axis", "date_days", "degree_by_far_label", "degree_hist", "int_values",
        "neighbor_degree_ratio")
IF_DECLARED = ("date_axis", "degree_by_far_label")   # flat only when the full file holds the field (its keys declare it)


def path_beside(full_stats):
    """the tier-1 file beside its full statistics file"""
    return os.path.join(os.path.dirname(os.path.abspath(full_stats)), FILE_NAME)


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def bucket(m):
    """floor(log2(ceil(m))) + 1, 0 for no edge (stats.cc degree_bucket)"""
    return 0 if m <= 0 else int(math.ceil(m)).bit_length()


def _degree_hist(d):
    """every owner at the mean degree of its owners row"""
    hist = {}
    for o in d["owners"]:
        n, own, e = d["type_counts"][o["owner_label"]], o["owners"], o["edges"]
        rows = [[0, n - own, 0, 0]]
        if own > 0:
            m = e / own
            rows.append([bucket(m), own, e, e * m])
        hist[f"{o['owner_label']}|{o['etype']}|{o['direction']}"] = rows
    return hist


def _date_days(d, full):
    """an even spread from the first day over the full file's number of days"""
    days = {}
    for k, h in sorted(full.get("date_days", {}).items()):
        lab, prop = k.split("|")
        u, span = d["sizes"]["content"][f"prop|{lab}|{prop}"]["u"], len(h["counts"])
        days[k] = {"first_day": h["first_day"], "counts": [u / span] * span}
    return days


def _int_values(d, full):
    """an even spread over value_range, one count per value"""
    iv = {}
    for k in sorted(full.get("int_values", {})):
        lo, hi = d["value_range"][k]
        kind, owner, _prop = k.split("|")
        holders = d["sizes"]["content"][k]["u"] if kind == "prop" else d["edge_totals"][owner]
        n = int(hi - lo) + 1
        iv[k] = {"first": lo, "counts": [holders / n] * n}
    return iv


def _degree_by_far_label(d, full):
    """the owners row's edges split over the declared far labels by vertex count, every owner at the mean degree"""
    far = {}   # O|E|dir -> its declared far labels
    for k in sorted(full.get("degree_by_far_label", {})):
        o, e, dr, f = k.split("|")
        far.setdefault(f"{o}|{e}|{dr}", []).append(f)
    rows = {f"{o['owner_label']}|{o['etype']}|{o['direction']}": o for o in d["owners"]}
    out = {}
    for key, fars in far.items():
        o = rows.get(key)
        weights = [d["type_counts"].get(f, 0) for f in fars]
        total = sum(weights)
        for f, w in zip(fars, weights):
            if o is None or o["owners"] <= 0:
                out[f"{key}|{f}"] = [0, 0]
                continue
            n = o["edges"] * (w / total if total > 0 else 1 / len(fars))
            out[f"{key}|{f}"] = [n, n * (o["edges"] / o["owners"])]
    return out


def _top_values(full, k):
    """each indexed property's list cut to its k most common values; holders kept (the rest's spread is derived from
    them and ndv at read time)"""
    return {key: {"k": k, "holders": t["holders"], "values": t["values"][:k]}
            for key, t in sorted(full.get("top_values", {}).items())}


def flat_fields(full):
    """the flat rows tier 1 writes for this full file: every one in FLAT, those in IF_DECLARED only when it holds them"""
    return sorted(f for f in FLAT if f not in IF_DECLARED or f in full)


def derive(full):
    """the tier-1 statistics (a dict) from the full ones: tier 2 removed, the flat rows added"""
    with open(CONTRACT) as f:
        contract = json.load(f)["properties"]
    tier2 = sorted(k for k, v in contract.items() if v.get("x-tier") == 2)
    d = {k: v for k, v in full.items() if k not in tier2}
    gone = [k for k in tier2 if k in full]
    if "date_axis" in full:
        d["date_axis"] = full["date_axis"]
    d["degree_hist"] = _degree_hist(d)
    d["neighbor_degree_ratio"] = {f"{e}|both": 1 for e in sorted(d["edge_totals"])}
    d["date_days"] = _date_days(d, full)
    d["burstiness"] = {k: 1 for k in sorted(full.get("burstiness", {}))}
    d["int_values"] = _int_values(d, full)
    if "top_values" in full:
        d["top_values"] = _top_values(full, contract["top_values"]["x-tier1-k"])
    if "degree_by_far_label" in full:
        d["degree_by_far_label"] = _degree_by_far_label(d, full)
    prov = dict(full.get("provenance", {}))
    prov["withheld"] = {"x-tier": 2, "fields": [f for f in gone if f not in FLAT]}
    prov["flat_tier2"] = {"fields": flat_fields(full),
                          "by": "kvevaluator.predict.tier1 (tier 1 + flat rows; date_days' first and last day only; "
                                "degree_by_far_label's keys only)"}
    d["provenance"] = prov
    return d


def write(full_path, out=None):
    """derive the tier-1 file from the full one and write it (default: beside it; the full file's hash in
    provenance.tier1_of); returns its path"""
    out = out or path_beside(full_path)
    with open(full_path) as f:
        d = derive(json.load(f))
    d["provenance"]["tier1_of"] = {"path": os.path.basename(full_path), "sha256": _sha256(full_path)}
    tmp = f"{out}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, out)
    return out


def for_tier(full_path, tier=DEFAULT_TIER):
    """the statistics file a prediction at `tier` reads: the full file (2), or the tier-1 file derived from it again
    and written beside it (1)"""
    if tier not in (1, 2):
        raise ValueError(f"tier {tier!r}: 1 or 2")
    return write(full_path) if tier == 1 else full_path
