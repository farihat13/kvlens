#!/usr/bin/env python3
"""The guard on a Python twin's counts (runner.run_plan + cost.TwinCountsRefused + Format.take_count_caveat).

Per-type CSR's Python twin is a rows-and-order oracle: it takes a vertex's label from the id's group and its
adjacency chunks hold no far-label block, while the C++ format reads the label a row carries or an entry stores.
run_plan must REFUSE the counts of every step where that can differ. It errs on the safe side: a step is refused
when the twin cannot know that C++ reads the same, so some refused steps do have equal counts. Checked here on a
SAMPLE of each reason (not every op: read_top_k_by_index, read_anchor_props under a list, read_edge_exists, chases,
closures and hops that read edge props have no case of their own; they go through the marked paths), on the sf0.1
Python oracle stores (no C++ store, no binary). That the UNREFUSED steps' counts equal the C++ format's is another
check: `check_cpp_vs_python.py --compare-counts` without the bypass (in `just cpp-diff`).
  a. a step of each kind the three reasons can touch is REFUSED, with that reason in its text: the label from the
     id's group or a plan list (hops from a message, props under a list, props under one label on a mixed group,
     both label filters), a chunk with a far-label block in C++ (likes out of a Person), the index layout
     (read_index_eq, read_index_range). The text names the format, the step, the op, the doc and the case it does not support
  b. the same run's ROWS are returned and equal the rows of a run under the bypass
  c. twin_counts="old-label-rule": the numbers come back and the sentence is printed exactly once per process
  d. clean steps return counts with no flag: a label scan, props under one label alone in its id group, a hop whose
     list has one such owner and one far label (knows)
  e. dgraph / nebula / arangodb: no step of the same hops and props reads is refused
  f. no file that uses a CountingKV reads a `.cost` outside runner.py, except the listed ones
  g. a total over the plan raises (sum, 0 +, ==, bool); a printed refused cost says REFUSED; verbose prints REFUSED
  h. writes: delete_vertex with and without its edges; an insert into and a delete from a list with a label block;
     an update of an indexed prop and an insert of a vertex with index entries; an update of a prop with no index
     keeps its counts
  i. an unknown twin_counts value is an error (no silent bypass by a typo)

    python3 tests/gates/check_twin_count_guard.py
    python3 tests/gates/check_twin_count_guard.py --readers-only    f alone (no store; the analysis-code gate level)
Exit 0 = every case held. The count of cases checked is printed; 0 is a failure.
"""
import contextlib
import io
import re
import sys
from pathlib import Path
from kvevaluator.paths import ROOT

EXP = ROOT
from kvevaluator.cost import KVCost, TwinCountsNotOracle, TwinCountsRefused
from kvevaluator.formats import make_format
from kvevaluator.plans.plan import Step, ref
from kvevaluator.oracle.run import build_stores
from kvevaluator.oracle.runner import run_plan, TWIN_COUNTS_BYPASS
from kvevaluator.kvstore.kvstore import CountingKV
from kvevaluator.kvstore.shadow import WriteShadowKV
from kvevaluator.schema.vids import vid
from kvevaluator.datasets import demo as data

SF = "0.1"
MARKED = ("pertype_csr",)
CLEAN = ("dgraph", "nebula", "arangodb")
# files that read a CountingKV's .cost themselves (not through run_plan): none reports a count. A new reader must go
# through run_plan or be listed with its reason
COST_READERS = {"python/kvevaluator/oracle/runner.py",   # run_plan itself: where the guard sits
                "tests/gates/check_write_effects.py",     # each format's write prediction against its own store, demo graph
                # base.counted: a read reports its store's count over the call as its cost; run_plan compares it, a
                # write adds it to its own prediction (no read formula exists)
                "python/kvevaluator/formats/base.py",
                "tests/gates/check_twin_count_guard.py"}  # this file (it names the pattern)

ok, bad = 0, []


def check(cond, what):
    global ok
    if cond:
        ok += 1
    else:
        bad.append(what)
        print(f"  FAIL {what}")


def refused(cost):
    return isinstance(cost, TwinCountsRefused)


def raises(fn):
    try:
        fn()
    except TwinCountsNotOracle as e:
        return str(e)
    return None


def plan(person, message, first_name):
    """(steps, {step: None for clean | a word its refusal text must hold})"""
    steps = [Step("scan", "read_vertices_by_label", label="TagClass", props=["name"]),
             Step("me", "read_vertex_props", vids=[person], props=["firstName"], label="Person"),
             Step("friends", "read_neighbors", srcs=[person], etype="knows", dir="both"),
             Step("fprops", "read_vertex_props", vids=ref("friends"), props=["firstName"], label="Person"),
             # label-rule steps: the label from the id's group, or a plan list tried in order
             Step("replies", "read_neighbors", srcs=[message], etype="replyOf", dir="in"),
             Step("author", "read_neighbors", srcs=[message], etype="hasCreator", dir="out"),
             Step("tags", "read_neighbors", srcs=[message], etype="hasTag", dir="out"),
             Step("rprops", "read_vertex_props", vids=ref("replies"), props=["creationDate"], label=["Post", "Comment"]),
             Step("as_post", "read_vertex_props", vids=ref("replies"), props=["creationDate"], label="Post"),
             # a chunk with a far-label block in C++ (likes out of a Person: one owner, the far end a Post or a Comment)
             Step("liked", "read_neighbors", srcs=[person], etype="likes", dir="out"),
             Step("lposts", "read_filter_by_label_probe", vids=ref("liked"), label="Post"),
             Step("lposts_ix", "read_filter_by_label_index", vids=ref("liked"), label="Post"),
             # the index layout
             Step("named", "read_index_eq", label="Person", prop="firstName", value=first_name),
             Step("dated", "read_index_range", labels=["Post"], prop="creationDate", lo=0, hi=1)]
    want = {"scan": None, "me": None, "friends": None, "fprops": None,
            "replies": "id's group", "author": "id's group", "tags": "id's group", "rprops": "id's group",
            "as_post": "id's group", "liked": "label block", "lposts": "id's group", "lposts_ix": "id's group",
            "named": "one key per entry", "dated": "one key per entry"}
    return steps, want


def cost_readers():
    """f. who reads a counting store's .cost directly: any file that names CountingKV and reads a `.cost`"""
    pat = re.compile(r"\.cost\b")
    readers = set()
    roots = [(EXP / sub, EXP) for sub in ("python/kvevaluator", "python/kvcost", "tests", "experiments")]
    for root, base in roots:
        for f in sorted(root.rglob("*.py")):
            rel = str(f.relative_to(base))
            if rel.startswith("python/kvevaluator/kvstore/"):
                continue                                     # the stores themselves
            text = f.read_text()
            code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(("from ", "import ")))
            if "CountingKV" in text and pat.search(code):
                readers.add(rel)
    return readers


def readers_only():
    """part f alone (no store): the analysis-code gate level runs it, since it scans python/kvcost and experiments/"""
    readers = cost_readers()
    extra = sorted(readers - COST_READERS)
    print(f"[twin-count-guard readers] {len(readers)} files read a counting store's .cost, {len(extra)} not listed"
          + (f": {extra}" if extra else ""))
    sys.exit(1 if extra else 0)


def main():
    from kvevaluator.oracle import runner
    from kvevaluator.datasets.ldbc.params import param_rows
    person = vid("Person", param_rows(SF, "ic2")[0]["personId"])
    messages = [vid("Post", r["messageId"]) for r in param_rows(SF, "is7")]
    first_name = param_rows(SF, "ic1")[0]["firstName"]
    schema = data.schema(SF)
    stores = build_stores(SF, list(MARKED + CLEAN), "abstract")
    probe = make_format(MARKED[0], schema)
    names = [p if isinstance(p, str) else p[0] for p in schema.vertex_prop_types["Person"]]
    # a Person prop no index covers (its update writes the props chunk only)
    plain_prop = next(p for p in names if p != "id" and not probe._update_index_cols("Person", {p: "x"}))
    # a message that has replies, so the props reads behind the hop have vids to read
    message = next((m for m in messages if probe.read_neighbors(stores[MARKED[0]][1], [m], "replyOf", "in")[0]), None)
    if message is None:
        sys.exit("no is7 param message with a reply at this scale: the test cannot cover props reads behind a hop")

    def run(name, steps, **kw):
        fmt = make_format(name, schema)
        return run_plan(steps, fmt, CountingKV(stores[name][1], sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes), **kw)

    steps, want = plan(person, message, first_name)
    nref = 0
    for name in MARKED:
        outs, kv = run(name, steps)
        by = {s.out: (p, c) for s, p, c in kv}
        # a. every label-rule, label-block and index step is refused, for the reason that applies to it
        for st, word in want.items():
            if word is None:
                # d. clean steps keep their counts
                check(isinstance(by[st][1], KVCost) and by[st][1].S >= 1, f"{name}: clean step {st} returns its counts")
                continue
            nref += 1
            check(refused(by[st][0]) and refused(by[st][1]), f"{name}: step {st} is refused")
            text = raises(lambda st=st: by[st][1].S) or ""
            check(word in text, f"{name}: step {st} is refused for the right reason ({word!r}; got {text[:150]!r})")
        text = raises(lambda: by["replies"][1].S) or ""
        check(name in text and "'replies'" in text and "read_neighbors" in text and "formats/pertype_csr.py" in text
              and "far-end label" in text, f"{name}: the refusal text names the format, the step, the op and where to "
              f"look (got: {text[:160]!r})")
        for lane in ("S", "G", "N", "scan_value_bytes", "total_bytes", "value_bytes"):
            check(raises(lambda l=lane: getattr(by["replies"][1], l)) is not None, f"{name}: reading {lane} raises")
        check(raises(lambda: by["replies"][0].triple()) is not None, f"{name}: the predicted cost's triple() raises")
        check(raises(lambda: bool(by["replies"][1])) is not None, f"{name}: bool(refused) raises (no `cost or default`)")
        # g. a total that includes a refused step is refused
        costs = [c for _s, _p, c in kv]
        check(raises(lambda: sum(c.S for c in costs)) is not None, f"{name}: sum of S over the plan raises")
        check(raises(lambda: 0 + by["replies"][1]) is not None, f"{name}: 0 + refused raises (sum's start)")
        check(raises(lambda: by["replies"][1] == by["replies"][0]) is not None, f"{name}: comparing a refused cost raises")
        check("REFUSED" in repr(by["replies"][1]), f"{name}: a printed refused cost says REFUSED")
        # b / c. the rows are the unguarded run's; the bypass returns numbers and says so EXACTLY once per process
        runner._said.discard(name)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outs2, kv2 = run(name, steps, twin_counts=TWIN_COUNTS_BYPASS)
            run(name, steps, twin_counts=TWIN_COUNTS_BYPASS)
        check(all(outs[k] == outs2[k] for k in outs), f"{name}: rows equal with and without the bypass")
        check(all(len(outs[k]) >= 1 for k in ("replies", "author", "liked")), f"{name}: the refused hops returned rows")
        by2 = {s.out: c for s, _p, c in kv2}
        check(all(isinstance(by2[st], KVCost) for st in want), f"{name}: the bypass returns every step's numbers")
        said = [l for l in buf.getvalue().splitlines() if l.startswith(f"[twin counts, {TWIN_COUNTS_BYPASS}]")]
        check(len(said) == 1 and name in said[0], f"{name}: the bypass prints the sentence exactly once ({len(said)} lines)")
        # verbose: a refused step prints REFUSED, not its counts
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run(name, steps, verbose=True)
        vl = [l for l in buf.getvalue().splitlines() if " replies " in l]
        check(len(vl) == 1 and "REFUSED" in vl[0] and "act S" not in vl[0],
              f"{name}: verbose prints REFUSED for a refused step, not its counts ({vl})")
        # i. no bypass by a typo
        try:
            run(name, steps[:1], twin_counts="yes")
            check(False, f"{name}: an unknown twin_counts value is an error")
        except ValueError:
            check(True, "")
        # h. writes
        fmt = make_format(name, schema)
        shadow = WriteShadowKV(stores[name][1])
        w = CountingKV(shadow, sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes, write_mode=True)

        def write(step):
            shadow.reset()
            return run_plan([step], fmt, w)[1][0][2]
        for with_edges in (True, False):                   # with_edges=False runs no hop: the step's own mark
            c = write(Step("del", "delete_vertex", vid=person, label="Person", with_edges=with_edges))
            check(refused(c), f"{name}: delete_vertex(with_edges={with_edges}) is refused as a whole step")
        c = write(Step("like", "insert_edge", etype="likes", src=person, dst=message, props={"creationDate": 1},
                       src_label="Person", dst_label="Post"))
        check(refused(c) and "label block" in (raises(lambda: c.S) or ""),
              f"{name}: an insert into likes (its out list stores far labels in C++) is refused for the block")
        c = write(Step("upd", "update_prop", vid=person, label="Person", props={plain_prop: "x"}))
        check(isinstance(c, KVCost), f"{name}: an update of a prop with no index ({plain_prop}) returns its counts")
        c = write(Step("upd", "update_prop", vid=person, label="Person", props={"firstName": "x"}))
        check(refused(c) and "one key per entry" in (raises(lambda: c.S) or ""),
              f"{name}: an update of an indexed prop is refused for the index layout")
        c = write(Step("ins", "insert_vertex", vid=vid("Person", 987654321987), label="Person", props={"firstName": "x"}))
        check(refused(c) and "one key per entry" in (raises(lambda: c.S) or ""),
              f"{name}: an insert of a vertex with index entries is refused for the index layout")
        c = write(Step("unlike", "delete_edge", etype="likes", src=person, dst=message, src_label="Person", dst_label="Post"))
        check(refused(c), f"{name}: a delete from likes (a list with a label block in C++) is refused")
    # e. the formats whose twins are count oracles: nothing refused (their plan: the hops and props reads)
    for name in CLEAN:
        _o, kv = run(name, [s for s in steps if s.out not in ("scan", "lposts", "lposts_ix", "named", "dated")])
        check(not any(refused(c) or refused(p) for _s, p, c in kv), f"{name}: no step refused")
    readers = cost_readers()
    extra = sorted(readers - COST_READERS)
    check(not extra, f"files that use a CountingKV and read a `.cost` outside run_plan, not on the list: {extra}")
    print(f"[twin-count-guard] {ok} cases held, {len(bad)} failed; marked formats {list(MARKED)} "
          f"({nref // len(MARKED)} of {len(steps)} plan steps refused each), clean {list(CLEAN)}; "
          f"{len(readers)} files read a counting store's .cost, {len(extra)} not listed")
    sys.exit(0 if ok and not bad else 1)


if __name__ == "__main__":
    readers_only() if sys.argv[1:] == ["--readers-only"] else main()
