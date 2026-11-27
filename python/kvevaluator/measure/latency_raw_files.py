"""Where cpplatency writes one raw response per (arm, query, param, regime), and how a reader finds them: the one
place that knows the layout, so the writer (measure/cpplatency.py) and the readers (--regen, kvcost measure-latency,
kvcost/latency/steps.py, w16_latency_score.py) cannot drift.

    <out>/<arm>/<query>/p<idx>/<regime>.json            a latency run: one body per query (the default, every regime)
    <out>/<arm>/<query>/<body>/p<idx>/<regime>.json     --compare-bodies: the bodies --candidates names for a query,
                                                        timed against each other (exploratory; the meta records
                                                        compare_bodies); a query it does not name runs its bundle and
                                                        stays at <query>/p<idx>/

A query's shape is its run's meta (steps.csv.meta.json beside the arms): a reader reads that shape only and refuses a
file in the other one.
"""
import glob
import json
import os

META = "steps.csv.meta.json"


def raw_path(out, arm, query, idx, regime, body=None):
    """the raw response file of one (arm, query, param, regime); body: the body's name in a --compare-bodies run only"""
    return os.path.join(out, arm, query, *([body] if body else []), f"p{idx}", f"{regime}.json")


def compare_bodies(out):
    """whether the cpplatency --out `out` is a --compare-bodies run (its meta says so; no meta: a one-body run). A meta
    from before the flag that names two bodies for a query is refused: its shape cannot be told from the meta"""
    path = os.path.join(out, META)
    if not os.path.exists(path):
        return False
    meta = json.load(open(path))
    if "compare_bodies" in meta:
        return bool(meta["compare_bodies"])
    many = sorted(q for q, names in (meta.get("candidates") or {}).items() if len(names) > 1)
    if many:
        raise SystemExit(f"latency raw files: {out}: a run from before --compare-bodies with several bodies for "
                         f"{many}: re-measure it with --compare-bodies")
    return False


def raw_globs(regime):
    """the globs of one regime's raw files below a dir of cpplatency outs: a one-body run's shape (a list, as the
    readers that try patterns in order take it)"""
    return [raw_path("", "*", "*", "*", regime)]


def _refuse_other_shape(out, arm, query, compare):
    """a one-body run holding a body folder under `query`, or a --compare-bodies run holding a file right under it"""
    other = sorted(glob.glob(raw_path(out, arm, query, "*", "*", body=None if compare else "*")))
    if other:
        raise SystemExit(f"latency raw files: {out} is {'a --compare-bodies' if compare else 'a one-body'} run but "
                         f"holds {len(other)} files of the other shape ({other[0]})")


def raw_file(out, arm, query, idx, regime):
    """the raw file of (arm, query, param, regime) in a one-body run, or None; a --compare-bodies run (its walls mix
    plans) and a body folder in a one-body run are refused"""
    if compare_bodies(out):
        raise SystemExit(f"latency raw files: {out} is a --compare-bodies run: its walls mix plans")
    _refuse_other_shape(out, arm, query, False)
    path = raw_path(out, arm, query, idx, regime)
    return path if os.path.isfile(path) else None


def raw_files(out, arm, regime):
    """{query: {idx: path}} of a one-body run's raw files of one regime; refused as raw_file refuses"""
    if compare_bodies(out):
        raise SystemExit(f"latency raw files: {out} is a --compare-bodies run: its walls mix plans")
    found = {}
    for path in sorted(glob.glob(raw_path(out, arm, "*", "*", regime))):
        parts = path.split(os.sep)
        found.setdefault(parts[-3], {})[parts[-2][1:]] = path
    arm_dir = os.path.join(out, arm)
    for query in sorted(os.listdir(arm_dir)) if os.path.isdir(arm_dir) else []:
        if os.path.isdir(os.path.join(arm_dir, query)):
            _refuse_other_shape(out, arm, query, False)
    return found


def body_folders(out, query):
    """whether `query`'s cells in the run `out` sit in body folders: a --compare-bodies run, a query its --candidates
    names"""
    if not compare_bodies(out):
        return False
    return query in (json.load(open(os.path.join(out, META))).get("candidates") or {})


def raw_cells(out, arm, query):
    """[(body or None, idx, regime, path)] of every raw cell of one query in its shape (body_folders), sorted by body,
    param and regime file name: what --regen walks"""
    compare = body_folders(out, query)
    _refuse_other_shape(out, arm, query, compare)
    cells = []
    for path in glob.glob(raw_path(out, arm, query, "*", "*", body="*" if compare else None)):
        parts = path.split(os.sep)
        cells.append((parts[-3] if compare else None, int(parts[-2][1:]), parts[-1], path))
    return [(b, i, fn[:-len(".json")], p) for b, i, fn, p in sorted(cells, key=lambda c: (c[0] or "", c[1], c[2]))]
