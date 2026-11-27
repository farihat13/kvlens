#!/usr/bin/env python3
"""Derive the two price-steps tables: one read of the microbenchmark (the paper's at SF3, so a reader can re-make it on
a small machine), its plan steps as measured in the three uniform layouts Sp-Sp, Co-Co and Ch64k-Ch64k. A layout
is priced one cell per step, by the setting it gives the step's part.
  - price-steps (figure price-steps.tex): who liked a person's posts (hop2_post_likers, param 3, plan "hand");
  - price-steps-fof (figure price-steps-fof.tex): the profiles of the persons among a person's
    friends of friends who posted (fof_posters_profile, param 3), three parts read at non-zero cost; also drawn as the
    table tables/price-steps-fof.tex, in weighted ops and bytes read.
The check, for each: Co-Ch64k priced from the three equals Co-Ch64k as built (measured itself beside the three), in weighted ops,
and for the second also in bytes read; the script stops if they differ. Weighted ops = 1.1 SEEK + GET + 0.01 NEXT; bytes
read = the seven read lanes, the miss keys included (step_record.READ_BYTE_LANES, the paper's "bytes read").

Reads the tree's microbench steps at the run's LDBC scale (captures/ldbc/<scale>/microbench/reported/steps: kvcost
measure --family microbench on the seven and Co-Ch64k: just results price-steps); writes <tables>/price-steps.csv and price-steps-fof.csv:
the query's step rows in Sp-Sp, Co-Co, Ch64k-Ch64k and Co-Ch64k
(layout: props_<p>_adj_<a>), every column of the steps file (S, G, N, the misses, the byte lanes); and
<tables>/price_steps.tex, the figures' numbers: price-steps.<sp|co|ch>.<step>.<seek|get|next|wops>,
price-steps.<coch|mixed>.total, and price-steps.fof.<sp|co|ch>.<knows|msgs|scan>.<seek|get|next|wops|bytes>,
price-steps.fof.<spsp|coco|chch|coch|mixed>.<total|bytes> (total = weighted ops), and the mixed layout against Ch64k-Ch64k:
price-steps.fof.mixed.chch.<wops-more|bytes-fewer>.

    uv run python paper/derive/price_steps.py [--from paper-results|output|<tree>] [--dataset ldbc=3] [--tables DIR]
"""
import argparse

from snapshots import add_tree_args, scale_in, size, tree_of, write_numbers, write_table
from kvevaluator.capture import capture_layout
from kvevaluator.layouts import SETTINGS, cell_id
from kvevaluator.output_tree import capture_in
from kvevaluator.capture.step_record import READ_BYTE_LANES

QUERY, PARAM, PLAN = "hop2_post_likers", "3", "hand"   # "hand": the plan file's body named "default" in older records
STEPS = ("ms", "po", "lk")             # the three plan steps the figure prices (the parts: hasCreator, Message, likes)
COCH = ("chunked", "condensed", "chunked")   # the setting Co-Ch64k gives the three steps' parts
LAYOUTS = SETTINGS
MIXED = ("split", "condensed", "chunked")    # the mixed layout the figure prices: hasCreator Split, Message Condensed, likes Chunked
SHORT = {"split": "sp", "condensed": "co", "chunked": "ch"}
MICROBENCH = SCALE = None    # the tree's microbench capture at the run's LDBC scale and that scale's dir name (main sets
                             # them): the three and Co-Ch64k measured
TABLE_LAYOUTS = [(s, s) for s in LAYOUTS] + [("condensed", "chunked")]   # the table: the three, and Co-Ch64k as built


def ops_text(v):
    """weighted operations as the figure prints them: one decimal below 100, else a whole number"""
    return f"{v:.1f}" if v < 100 else f"{round(v):,}"


def steps_of(capture, props, adj):
    """(columns, the query's step rows) in the uniform layout props x adj of a microbench capture"""
    with open(capture.path_of("steps", cell_id(props, adj)), newline="") as fh:
        reader = capture_layout.reader(fh)
        rows = [r for r in reader if (r["query"], r["param_idx"], r["body"]) == (QUERY, PARAM, PLAN)]
        if not rows:
            raise SystemExit(f"{capture.path}: no step rows of {QUERY} param {PARAM} plan {PLAN} in {cell_id(props, adj)}")
        return list(reader.fieldnames), rows


def wops(r):
    return 1.1 * int(r["S"]) + int(r["G"]) + 0.01 * int(r["N"])


def coch_check():
    """Co-Ch64k priced from the three uniform layouts equals Co-Ch64k as built, on SF3's capture (all nine measured there)"""
    sf3 = {f"{p}_{a}": steps_of(MICROBENCH, p, a)[1] for p, a in [(s, s) for s in LAYOUTS] + [("condensed", "chunked")]}
    step = lambda layout, st: next(r for r in sf3[layout] if r["step"] == st)
    priced = sum(wops(step(f"{s}_{s}", st)) for s, st in zip(COCH, STEPS))
    built = sum(wops(r) for r in sf3["condensed_chunked"] if r["step"] in STEPS)
    if abs(priced - built) > 1e-6:
        raise SystemExit(f"{MICROBENCH.path}: Co-Ch64k priced from the three ({priced}) is not Co-Ch64k as built ({built})")
    print(f"{SCALE} Co-Ch64k: priced {priced:.2f}, built {built:.2f} (the check)")


# the alternative figure: the friends-of-friends posters' profiles, one plan; three columns, one per
# part, each the sum of the steps that read it (the "posts" step, keep the Posts by label, reads nothing: the label comes
# with the adjacency entry)
FOF = {"query": "fof_posters_profile", "param": "3", "plan": "fof_posters_profile_from_person_run(hand)",
       "columns": {"knows": ("f1", "f2"), "msgs": ("msgs",), "scan": ("scan",)},
       "kind": {"knows": "adj", "msgs": "adj", "scan": "props"},       # whose setting picks the column's cell
       "coch": {"knows": "chunked", "msgs": "chunked", "scan": "condensed"},
       "mixed": {"knows": "condensed", "msgs": "chunked", "scan": "chunked"}}


def fof_rows(capture, props, adj):
    with open(capture.path_of("steps", cell_id(props, adj)), newline="") as fh:
        rows = [r for r in capture_layout.reader(fh)
                if (r["query"], r["param_idx"], r["body"]) == (FOF["query"], FOF["param"], FOF["plan"])]
    if not rows:
        raise SystemExit(f"{capture.path}: no step rows of {FOF['query']} param {FOF['param']} plan {FOF['plan']} in {cell_id(props, adj)}")
    return rows


def fof_column(rows, col):
    """(S, G, N, wops, bytes read) of the steps a column sums"""
    rs = [r for r in rows if r["step"] in FOF["columns"][col]]
    if len(rs) != len(FOF["columns"][col]):
        raise SystemExit(f"{FOF['query']}: steps {FOF['columns'][col]} not all found ({[r['step'] for r in rs]})")
    S, G, N = (sum(int(r[k]) for r in rs) for k in ("S", "G", "N"))
    return S, G, N, sum(wops(r) for r in rs), sum(int(r[lane]) for r in rs for lane in READ_BYTE_LANES)


def fof_priced(cells, setting, i=3):
    """a layout priced one cell per column, by the setting it gives the column's part: weighted ops (i=3) or bytes (i=4)"""
    return sum(fof_column(cells[(setting[c], setting[c])], c)[i] for c in FOF["columns"])


def fof_keys(keys):
    """the alternative figure's keys, price-steps.fof.*, from SF3's three uniform layouts, and its Co-Ch64k check"""
    sf3 = {(s, s): fof_rows(MICROBENCH, s, s) for s in LAYOUTS}
    coch = {c: (FOF["coch"]["scan"], FOF["coch"][c]) if FOF["kind"][c] == "adj" else (FOF["coch"][c], FOF["coch"]["knows"])
            for c in FOF["columns"]}
    built_rows = fof_rows(MICROBENCH, "condensed", "chunked")
    built = sum(fof_column(built_rows, c)[3] for c in FOF["columns"])
    built_bytes = sum(fof_column(built_rows, c)[4] for c in FOF["columns"])
    priced, priced_bytes = fof_priced(sf3, FOF["coch"]), fof_priced(sf3, FOF["coch"], 4)
    if abs(priced - built) > 1e-6 or priced_bytes != built_bytes:
        raise SystemExit(f"{MICROBENCH.path} {FOF['query']}: Co-Ch64k priced from the three ({priced} wops, {priced_bytes} B) is not Co-Ch64k as "
                         f"built ({built} wops, {built_bytes} B) ({coch})")
    print(f"{SCALE} {FOF['query']} Co-Ch64k: priced {priced:.2f} wops {priced_bytes:,} B, built {built:.2f} wops {built_bytes:,} B (the check)")
    for s in LAYOUTS:
        for c in FOF["columns"]:
            S, G, N, w, b = fof_column(sf3[(s, s)], c)
            key = f"price-steps.fof.{SHORT[s]}.{c}"
            keys.update({f"{key}.seek": f"{S:,}", f"{key}.get": f"{G:,}", f"{key}.next": f"{N:,}", f"{key}.wops": ops_text(w),
                         f"{key}.bytes": size(b)})
    uniform = {f"{SHORT[s]}{SHORT[s]}": {c: s for c in FOF["columns"]} for s in LAYOUTS}   # spsp, coco, chch
    for name, setting in {**uniform, "coch": FOF["coch"], "mixed": FOF["mixed"]}.items():
        keys[f"price-steps.fof.{name}.total"] = ops_text(fof_priced(sf3, setting))
        keys[f"price-steps.fof.{name}.bytes"] = size(fof_priced(sf3, setting, 4))
    # the mixed layout against Ch64k-Ch64k, the fewest weighted ops: more ops, fewer bytes (the caption's trade-off)
    keys["price-steps.fof.mixed.chch.wops-more"] = f"{round(fof_priced(sf3, FOF['mixed']) - fof_priced(sf3, uniform['chch'])):,}"
    keys["price-steps.fof.mixed.chch.bytes-fewer"] = size(fof_priced(sf3, uniform["chch"], 4) - fof_priced(sf3, FOF["mixed"], 4))
    return [{"layout": cell_id(p, a), **r} for p, a in TABLE_LAYOUTS for r in fof_rows(MICROBENCH, p, a)]


def main():
    global MICROBENCH, SCALE
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_tree_args(ap)
    args = ap.parse_args()
    scale = scale_in(args, "ldbc")
    if scale is None:
        raise SystemExit("price-steps reads the microbenchmark on LDBC: the run (--dataset) leaves ldbc out")
    captures = tree_of(args.tree).place_dir("captures", "ldbc", scale)
    MICROBENCH, SCALE = capture_in(captures, "microbench"), captures.name
    coch_check()
    out = {}
    for p, a in TABLE_LAYOUTS:
        columns, rows = steps_of(MICROBENCH, p, a)
        out[cell_id(p, a)] = rows
    step = lambda layout, st: next(r for r in out[layout] if r["step"] == st)
    priced = sum(wops(step(cell_id(s, s), st)) for s, st in zip(COCH, STEPS))
    # the paper's table keeps its released header (`variant`, the column's other name) until the paper's next
    # release of evidence/price-steps; the figure does not read that column
    paper = lambda c: capture_layout.OLD_BODY if c == capture_layout.BODY else c
    paper_row = lambda r: {paper(k): v for k, v in r.items()}
    rows = [{"layout": layout, **{paper(k): v for k, v in r.items()}} for layout, rs in out.items() for r in rs]
    write_table("price-steps", ["layout"] + [paper(c) for c in columns], rows, key=("layout", "step_idx", "step"),
                out_dir=args.tables)
    keys = {}
    for s in LAYOUTS:
        for st in STEPS:
            r = step(cell_id(s, s), st)
            key = f"price-steps.{SHORT[s]}.{st}"
            keys.update({f"{key}.seek": f"{int(r['S']):,}", f"{key}.get": f"{int(r['G']):,}",
                         f"{key}.next": f"{int(r['N']):,}", f"{key}.wops": ops_text(wops(r))})
    keys["price-steps.coch.total"] = f"{priced:,.1f}"
    keys["price-steps.mixed.total"] = f"{sum(wops(step(cell_id(s, s), st)) for s, st in zip(MIXED, STEPS)):,.1f}"
    fof = fof_keys(keys)
    write_table("price-steps-fof", ["layout"] + [paper(c) for c in columns], [{k: paper_row(r).get(k, "") for k in ["layout"] + [paper(c) for c in columns]} for r in fof],
                key=("layout", "step_idx", "step"), out_dir=args.tables)
    write_numbers("price_steps", keys, f"captures/ldbc/{captures.name}, microbench steps, {QUERY} and "
                  f"{FOF['query']} param {PARAM}; Co-Ch64k priced = built checked; weighted operations = 1.1 SEEK + GET + 0.01 NEXT",
                  out_dir=args.tables)


if __name__ == "__main__":
    main()
