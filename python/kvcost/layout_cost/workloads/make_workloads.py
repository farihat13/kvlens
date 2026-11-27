#!/usr/bin/env python3
"""The workloads written from a recipe, and the one writer of a workload file (inputs/workloads/<benchmark>/[<scale>/]<id>.json).

A workload file is the one copy of a workload: benchmarks.workloads() reads it. W01-W15 are written by hand (their
files are the source); the others are computed from a benchmark's own inputs, so their recipe lives here and their files
are this script's output, checked by --check:
  I01, I02   LDBC Interactive's own mix (inputs/frequencies/ldbc/sf3/mix.json, gen_ldbc_mix.py), and its reads only
  L01-L04    LinkBench's request-log frequencies at a scale (inputs/frequencies/linkbench/<scale>/mix.json) and variants
  J01        the Join Order Benchmark's 113 queries, each once (the plan files' query list)
  B01        LDBC BI's queries, each once (the plan files' query list, in number order)
A file: id, name (the workload's name in the CSVs), title, items (each: family, query, frequency, params; params = the
1-based param indexes the item covers, null = the mean over all), then the workload's other fields as written.

    python3 python/kvcost/layout_cost/workloads/make_workloads.py [--dataset ldbc|linkbench|job|bi] [--scale n1m] [--check]
"""
import argparse
import json
import os
import sys

from kvevaluator.paths import INPUTS_DIR
from kvevaluator.datasets import frequencies_in
from kvcost.layout_cost.workloads.ldbc import DRIVER_READ_FACTOR, LDBC, PER, W
from kvcost.layout_cost.workloads.linkbench import LB, LW, READS, WRITES

WORKLOADS_DIR = INPUTS_DIR / "workloads"
ITEM_FIELDS = ("family", "query", "frequency", "params")


def to_json(name, w):
    """a workload as its file holds it (text, ending in a newline)"""
    out = {"id": w["id"], "name": name, "title": w["title"], "items": "@ITEMS@"}
    out.update({k: v for k, v in w.items() if k not in ("id", "title", "items")})
    items = ",\n".join("  " + json.dumps(dict(zip(ITEM_FIELDS, it)), ensure_ascii=False) for it in w["items"])
    text = json.dumps(out, indent=1, ensure_ascii=False)
    return text.replace('"@ITEMS@"', "[\n" + items + "\n ]", 1) + "\n"      # one item per line


# --- I01, I02: LDBC Interactive's own mix ---------------------------------------------------------------------------
# The scale whose LDBC frequencies I01 and I02 use, at every analysis scale (python/kvcost/layout_cost/workloads/ldbc.py).
# A workload from another scale's frequencies is one more recipe: i01(<scale>) under its own id, after
# gen_ldbc_mix.py --sf <scale> wrote inputs/frequencies/ldbc/sf<scale>/mix.json.
W16_FREQUENCIES = "3"


def ldbc_mix(freq_scale):
    with open(frequencies_in("ldbc", freq_scale) / "mix.json") as f:
        return json.load(f)


def _items(freq_scale):
    """I01's items: (family, query, count per 100,000 updates, params)"""
    m = ldbc_mix(freq_scale)
    ic = {q: round(PER * v) for q, v in sorted(m["complex_per_update"].items(), key=lambda kv: int(kv[0][2:]))}
    # the driver starts a short-read walk after EVERY complex read, IC13/IC14 included (LdbcSnbShortReadGenerator:
    # shortReadFactories[LdbcQuery13/14.TYPE] = randomFirstQuery): out of scope as queries, their walks are IS1-IS7
    all_complex = sum(m["complex_per_update"].values()) + sum(m["complex_per_update_out_of_scope"].values())
    each_is = round(m["short_reads_per_complex_each"] * all_complex * PER)
    iu = {q: round(PER * v) for q, v in sorted(m["update_share"].items(), key=lambda kv: int(kv[0][2:]))}
    items = ([(LDBC, q, n, None) for q, n in ic.items()]
             + [(LDBC, f"is{i}", each_is, None) for i in range(1, 8)]
             + [(W, q, n, None) for q, n in iu.items() if n > 0])
    return items


def i01(freq_scale=W16_FREQUENCIES):
    items, m = _items(freq_scale), ldbc_mix(freq_scale)
    sf = f"sf{freq_scale}"
    walk = m["short_reads_per_complex_each"]
    complex_per_update = sum(m["complex_per_update"].values()) + sum(m["complex_per_update_out_of_scope"].values())
    return {
        "id": "i01",
        "title": f"LDBC Interactive, LDBC frequencies ({sf}'s)",
        "items": items,
        "sweep": [q for _, q, _, _ in items if q.startswith("iu")],
        "param_spread": [f"is{i}" for i in range(1, 8)],   # 5 fixed params each; the driver takes IC results
        # extra marks on the sweep: (multiplier of the stated write:read ratio, label)
        "sweep_marks": [(1 / DRIVER_READ_FACTOR, f"the driver's time pacing ({DRIVER_READ_FACTOR:.2f}x the reads)")],
        "method": f"LDBC's driver config and {sf} update stream (python/kvevaluator/workloads/interactive/gen_ldbc_mix.py). Complex reads IC1-IC12: one run "
                  f"per freq_i updates ({sf}.properties; IC13/IC14 have no plans here). Short reads IS1-IS7: the driver's "
                  f"random walk after EVERY complex read, IC13/IC14 included ({walk:.3f} runs of each IS per complex read, "
                  f"{complex_per_update:.3f} complex reads per update; an upper bound -- a walk that finds no ids ends early). Updates "
                  f"IU1-IU8: their shares of the {sf} update stream. Frequencies are the benchmark's stated intent: the "
                  "driver paces reads by time, and on these stream files it would issue ~0.62x the complex reads per "
                  f"update -- the write:read sweep covers that range. The {sf} prescription is used at every SF.",
        "expect": "Condensed props (IS1/IS4/IS5 read whole vertices; IU inserts write whole vertices). Lists split by "
                  "edge type: knows (read by most ICs, written by IU8 only, 1% of updates) Condensed or Chunked; the "
                  "write-heavy lists -- hasMember (IU5, 49% of updates), likes (IU2/IU3, 29%), the Comment/Post edges "
                  "(IU6/IU7) -- Split in bytes. So the best MIXED layout should beat every uniform one on at least one "
                  "currency without losing the other (a real test of the rule: parts carry different traffic). IC5 "
                  "reads hasMember, so hasMember is the contested part.",
    }


def i02():
    """I01 without its updates: the same complex and short reads at the same frequencies"""
    items = [it for it in _items(W16_FREQUENCIES) if it[0] == LDBC]
    return {
        "id": "i02",
        "title": "LDBC Interactive, LDBC frequencies, reads only (I01 without its updates)",
        "items": items,
        "param_spread": [f"is{i}" for i in range(1, 8)],
        "method": "I01's complex reads IC1-IC12 and short reads IS1-IS7, at I01's counts (per 100,000 updates of "
                  "I01); the updates IU1-IU8 are left out. LDBC ships no read-only mix: this is its own read mix, "
                  "as L02 is LinkBench's. The short-read rate is an upper bound, as in I01.",
        # fixed before pricing I02 at any SF (I01's results were known)
        "expect": "The same two ends as I01: All-Chunked cheapest in ops, Split/Condensed in bytes (reads are 95% "
                  "of I01's bytes in Split/Condensed). At the ends the best mixed layout gains at most 2% in either "
                  "currency: I01's 4% bytes gain came from two write lists, which I02 does not write. Within "
                  "the reference layout's bytes (B = 1) a mix still pays, about as much as on I01 (3.5x fewer ops than the "
                  "best single layout that fits, at SF30): the budget picks which read lists to chunk.",
    }


# --- L01-L04: LinkBench's request log --------------------------------------------------------------------------------
# the single layouts a why text names (its numbers are computed per scale by why_numbers.py)
SS, DG, NB, CC, CH = ("props_split_adj_split", "props_split_adj_condensed", "props_condensed_adj_split",
                      "props_condensed_adj_condensed", "props_chunked_adj_chunked")


def linkbench_mix(scale):
    """the request log's per-query counts at a scale: the tracked copy of its mix.json (gen_linkbench_params.py --track)"""
    path = frequencies_in("linkbench", scale) / "mix.json"
    if not path.exists():
        raise SystemExit(f"no {path.relative_to(INPUTS_DIR.parent)}: generate the scale's params, then track them "
                         f"(python/kvevaluator/datasets/linkbench/gen_linkbench_params.py --scale {scale} --track)")
    with open(path) as f:
        return json.load(f)["count"]


def linkbench(scale, counts=None):
    """the L workloads at `scale` (counts: the request log's per-query counts, default its tracked mix.json)"""
    counts = counts if counts is not None else linkbench_mix(scale)
    reads = [(LB, q, counts[q], None) for q in READS if counts.get(q)]
    writes = [(LW, q, counts[q], None) for q in WRITES if counts.get(q)]
    return {
        "lb_default": {
            "id": "l01", "title": "LinkBench, its log's frequencies",
            "items": reads + writes, "sweep": [q for _, q, _, _ in writes],
            "expect": "Condensed props (getNode 13% + updateNode 7%: 1 GET / 1 row write vs Split's 4 GETs / 3 PUTs); "
                      "Split or Condensed adjacency in ops (a typical list is short, so every layout reads it for about "
                      "one SEEK, while Chunked pays a chunk read-and-rewrite on the ~20% of requests that write a "
                      "list); Split adjacency in bytes. The best mixed layout == the best uniform one (P1): every part "
                      "carries reads and writes of the same kind.",
            "why": "Node props: Condensed. getNode reads a node in 1 GET (Split: 4, one per field), updateNode rewrites the row in 1 GET + 1 PUT (Split: 3 PUTs), addNode writes 2 keys (Split: 5). Lists: a typical list is short (the median getLinkList returns one link or none), so every layout reads it for about one SEEK. All-Condensed is cheapest in ops ({cc_ops}; Condensed/Split {nb_ops}), and a Condensed list write pays the read of the old list it makes (1 SEEK). It wins because of one hub node: it gets only a few requests, but on Split lists each of them walks the whole list. Without the hub Condensed/Split is cheaper in ops (sensitivity.csv). In bytes Condensed lists win (Split/Condensed {dg_bytes}, All-Condensed {cc_bytes}; Condensed/Split {nb_bytes}): with the schema codec a list entry carries values only. The hub carries most of every non-Chunked layout's bytes; without it Split props + Split lists is cheapest (sensitivity.csv). All-Chunked reads and writes {ch_x} the bytes of the cheapest layout, and no layout with a Chunked part is the cheapest in ops. The best mixed layout is a uniform one: link1 and link2 carry the same traffic, so a mix only interpolates between two uniform layouts.",
            "why_numbers": {"cc_ops": ("total", "ops", CC), "nb_ops": ("total", "ops", NB), "dg_bytes": ("total", "bytes", DG), "cc_bytes": ("total", "bytes", CC), "nb_bytes": ("total", "bytes", NB), "ch_x": ("ratio", ("total", "bytes", CH), ("total", "bytes", "best_single_bytes"))},
            "why_scale_free": {"1": "a Condensed row or list is one key (one GET / SEEK, one PUT)", "4": "a node has 4 stored fields: one key each in Split", "3": "updateNode rewrites 3 fields", "2": "a node row + its index entry", "5": "4 field keys + the index entry"},
        },
        "lb_reads": {
            "id": "l02", "title": "LinkBench's reads only",
            "items": reads,
            "expect": "Condensed props (getNode); adjacency near-tied in ops (short lists), Chunked cheapest only if "
                      "hubs are sampled, Split/Condensed cheapest in bytes. Mixed == uniform (reads alone never split).",
            "why": "Reads only: All-Condensed is cheapest in ops and bytes ({cc_ops} ops, {cc_bytes}), in every sensitivity case: 1 GET per node, 1 SEEK per list where Split adds one NEXT per link. With the hub, Condensed/Split pays {nb_more} more ops; without it, much less (sensitivity.csv).",
            "why_numbers": {"cc_ops": ("total", "ops", CC), "cc_bytes": ("total", "bytes", CC), "nb_more": ("share", ("diff", ("total", "ops", NB), ("total", "ops", CC)), ("total", "ops", CC))},
            "why_scale_free": {"1": "a Condensed row or list is one key"},
        },
        "lb_writes": {
            "id": "l03", "title": "LinkBench's writes only",
            "items": writes,
            "expect": "Split adjacency (2 small keys vs a list or chunk rewrite); Condensed props in ops (1 row write "
                      "vs 3 puts), Split props in bytes. Mixed == uniform.",
            "why": "Writes only: Condensed/Split (Condensed props, Split lists) is cheapest in ops ({nb_ops}; All-Condensed {cc_ops}, {cc_more} more: a Condensed list write reads and rewrites the list), Split props + Split lists in bytes ({ss_bytes}: no row read-back, 2 small keys per link). Condensed props win ops (row writes), Split props bytes.",
            "why_numbers": {"nb_ops": ("total", "ops", NB), "cc_ops": ("total", "ops", CC), "cc_more": ("share", ("diff", ("total", "ops", CC), ("total", "ops", NB)), ("total", "ops", NB)), "ss_bytes": ("total", "bytes", SS)},
            "why_scale_free": {"2": "one record per direction of a link"},
        },
        "lb_writes_x3": {
            "id": "l04", "title": "LinkBench with three times the writes",
            "items": reads + [(f, q, 3 * n, p) for f, q, n, p in writes],
            "expect": "Between L01 and L03: Split adjacency in ops and bytes; Condensed props in ops. Mixed == uniform.",
            "why": "Three times the writes: Condensed/Split is cheapest in ops ({nb_ops}; All-Condensed {cc_ops}, {cc_more} more), Split/Condensed (Split props, Condensed lists) in bytes ({dg_bytes}). Without the hub Split props + Split lists is cheapest in bytes (sensitivity.csv).",
            "why_numbers": {"nb_ops": ("total", "ops", NB), "cc_ops": ("total", "ops", CC), "cc_more": ("share", ("diff", ("total", "ops", CC), ("total", "ops", NB)), ("total", "ops", NB)), "dg_bytes": ("total", "bytes", DG)},
        },
    }

# --- J01: the Join Order Benchmark -----------------------------------------------------------------------------------
def job():
    """the J workloads"""
    from kvevaluator.plan_choice.candidates import workload_queries
    queries, _ = workload_queries("job")
    return {"job_all": {"id": "j01", "title": "The Join Order Benchmark, every query once",
                        "items": [("job", q, 1.0, None) for q in queries]}}


# --- B01: LDBC BI, every query once ---------------------------------------------------------------------------------
def bi():
    """the B workloads: BI's queries in number order (the combined panel's order). B01 sits with LDBC's workloads; a load
    of the suite leaves it out unless asked (benchmarks.workloads: BI is an optional family)"""
    from kvevaluator.plan_choice.candidates import workload_queries
    queries, _ = workload_queries("bi")
    queries = sorted(queries, key=lambda q: (int("".join(ch for ch in q if ch.isdigit())), q))
    return {"bi_all": {"id": "b01", "title": "LDBC BI, every query once", "items": [("bi", q, 1.0, None) for q in queries]}}


def recipes(dataset, scale=None):
    """{relative path under inputs/workloads: file text} of the workloads a recipe writes"""
    if dataset == "ldbc":           # LDBC's mixes, and BI's (a family a load adds when asked: its own folder)
        ws = {"ldbc_mix": i01(), "ldbc_mix_reads": i02()}
        return {**{f"ldbc/{w['id']}.json": to_json(n, w) for n, w in ws.items()},
                **{f"ldbc/{w['id']}.json": to_json(n, w) for n, w in bi().items()}}
    if dataset == "linkbench":
        if not scale:
            raise SystemExit("--dataset linkbench needs --scale (its request log is per scale)")
        return {f"linkbench/{scale}/{w['id']}.json": to_json(n, w) for n, w in linkbench(scale).items()}
    if dataset == "imdb":
        return {f"imdb/{w['id']}.json": to_json(n, w) for n, w in job().items()}
    raise SystemExit(f"--dataset {dataset!r}: ldbc, linkbench or imdb")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", default="all", help="ldbc (with BI's), linkbench, imdb, or all (linkbench: at --scale, else at "
                    "every scale with a tracked mix.json)")
    ap.add_argument("--scale", default=None, help="LinkBench's scale tag (n1m)")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file differs from its recipe")
    a = ap.parse_args()
    names = ["ldbc", "imdb", "linkbench"] if a.dataset == "all" else [a.dataset]
    # LinkBench's recipe is per scale: the one asked, else every scale whose request-log counts are tracked
    lb_scales = [a.scale] if a.scale else sorted(d.name for d in frequencies_in("linkbench").glob("*")
                                                   if (d / "mix.json").exists())
    files = {p: t for b in names for sc in (lb_scales if b == "linkbench" else [None]) for p, t in recipes(b, sc).items()}
    differ = []
    for rel, text in sorted(files.items()):
        path = WORKLOADS_DIR / rel
        old = path.read_text() if path.exists() else None
        if old == text:
            continue
        differ.append(rel)
        if not a.check:
            os.makedirs(path.parent, exist_ok=True)
            path.write_text(text)
    what = "differ from their recipe" if a.check else "written"
    shown = [f"linkbench ({', '.join(lb_scales) or 'no tracked scale'})" if b == "linkbench" else b for b in names]
    print(f"[make_workloads] {len(files)} files from {', '.join(shown)}; {len(differ)} {what}"
          + (f": {', '.join(differ)}" if differ else ""))
    return 1 if (a.check and differ) or not files else 0


if __name__ == "__main__":
    sys.exit(main())
