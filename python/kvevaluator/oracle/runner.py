"""Run a plan against a (format, store); collect predicted vs actual per step."""
from kvevaluator.cost import TwinCountsRefused
from kvevaluator.plans.plan import Ref


def _resolve(args, outputs):
    return {k: (outputs[v.name] if isinstance(v, Ref) else v) for k, v in args.items()}


TWIN_COUNTS_BYPASS = "old-label-rule"      # run_plan(twin_counts=...): the one named way to read a refused step's counts
_SEE = ("use the C++ evaluator (Per-type CSR's Python twin is a rows oracle: python/kvevaluator/formats/pertype_csr.py, "
        "its docstring; "
        "not supported: the Python twin following the stored far-end label)")
_said = set()


def run_plan(plan, fmt, store, verbose=False, twin_counts=None):
    """Execute each step in order. Returns (outputs, kvcosts) where kvcosts is a list
    of (step, predicted, actual); `actual` is measured by the CountingKV `store`.
    A step whose counts the format disowns (Format.take_count_caveat) comes back with cost.TwinCountsRefused in place
    of both costs: its rows are right, its counts raise when read. twin_counts="old-label-rule" returns the numbers
    anyway and prints the reason once (a tool that measures the twin's own rule, or checks the twin against itself)."""
    if twin_counts not in (None, TWIN_COUNTS_BYPASS):
        raise ValueError(f"twin_counts={twin_counts!r}: the only named bypass is {TWIN_COUNTS_BYPASS!r}")
    outputs, kvcosts = {}, []
    if hasattr(fmt, "begin_plan"):          # a format's per-plan state (nebula: whether a vertex write ran)
        fmt.begin_plan()
    for step in plan:
        args = _resolve(step.args, outputs)
        method = getattr(fmt, step.op)
        store.reset_counter()
        fmt.take_count_caveat()             # nothing carried over from an earlier step
        result, predicted = method(store, **args)
        actual = store.cost.copy()          # this step's tally (reset above, filled by method)
        caveat = fmt.take_count_caveat()
        why = None
        if caveat:
            why = (f"{fmt.name}, step '{step.out}' ({step.op}): {caveat}, so this step's Python S / G / N and bytes are "
                   f"not the format's counts; {_SEE}")
            if twin_counts == TWIN_COUNTS_BYPASS:
                if fmt.name not in _said:
                    _said.add(fmt.name)
                    print(f"[twin counts, {TWIN_COUNTS_BYPASS}] {why}  (said once; every such step of {fmt.name} "
                          f"in this run reads the twin's own counts)", flush=True)
                why = None                                   # the caller asked for the twin's own numbers
        if verbose and why:
            print(f"  {step.op:11s} {step.out:8s} | counts REFUSED: {caveat}")
        elif verbose:
            # a superset of print_report: per-step cost, the oracle verdict, AND the
            # step's result (small results in full; big ones as a row count).
            ok = "OK" if predicted.triple() == actual.triple() else "MISMATCH"
            # show the first 3 rows if it's a list, else the whole result (dicts are
            # usually small); append "..." when there are more than 3 rows.
            if isinstance(result, list):
                shown = result[:3] if len(result) > 3 else result
                if len(result) > 3:
                    shown.append("...")
            else:
                shown = result
            print(f"  {step.op:11s} {step.out:8s} | "
                  f"pred S{predicted.S} G{predicted.G} N{predicted.N}  "
                  f"act S{actual.S} G{actual.G} N{actual.N}  "
                  f"b{actual.value_bytes:<5d} | {ok}")
            if isinstance(shown, list):
                print("    result = [")
                for r in shown:
                    print(f"      {r},")
                print("    ]")
            else:
                print(f"    result = {shown}")
        outputs[step.out] = result
        if why:
            predicted = actual = TwinCountsRefused(why)
        kvcosts.append((step, predicted, actual))
    return outputs, kvcosts


def execute(plan, fmt, store):
    """Run a plan for its RESULT only, with no cost accounting -- works against a raw
    backend with zero CountingKV/sizeof overhead. Use this for timing
    so the byte-accounting doesn't pollute the measured wall-clock; get the cost
    vector from a separate run_plan against a CountingKV."""
    outputs = {}
    for step in plan:
        args = _resolve(step.args, outputs)
        outputs[step.out], _predicted = getattr(fmt, step.op)(store, **args)
    return outputs


def ingest_stream(records, fmt, store):
    """Stream ('v'/'e', ...) records into a format's store, then finalize_write. No full
    in-memory graph -- the loader feeds one record at a time (the scale path)."""
    for rec in records:
        if rec[0] == "v":
            _, vid, label, props = rec
            fmt.add_vertex(store, vid, label, props)
        else:
            _, etype, src, dst, props, src_label, dst_label = rec
            fmt.add_edge(store, etype, src, dst, props, src_label, dst_label)
    fmt.finalize_write(store)


def print_report(name, kvcosts):
    print(f"\n=== {name} ===")
    for step, pred, act in kvcosts:
        ok = "OK" if pred.triple() == act.triple() else "MISMATCH"
        print(f"  {step.op:11s} {step.out:8s} | "
              f"pred S{pred.S} G{pred.G} N{pred.N}  act S{act.S} G{act.G} N{act.N}"
              f"  b{act.value_bytes:<4d} | {ok}")
