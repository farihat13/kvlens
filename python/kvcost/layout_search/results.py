"""The mix search's result file: one JSON file per (dataset, scale, workload, option set), one row per question asked of
it, upserted by key. Every figure, number and spec reads this file; nothing re-solves to draw.

A row's key (KEY) is every input that changes its answer: the benchmark, the question, the model, the limit, the
weighting, the workload's frequencies (a hash of its items: frequencies() below), the plan pick criterion, the plan rule (its pick record as a path in the repo), the node limit, the walk's
time limit (--max-seconds), the search method (--search), the capture root and the other chunk sizes' captures
(per-type-any-size). The solver (the cost cache, or no cost cache) is no key: it records how a row was found, and both
give the same answer, so a second run of the same question replaces its row whichever solver ran; anything else adds
one. A file holds one
workload's rows at one set of frequencies: rows at other frequencies (another workload file searched into the same
rows) are refused on write, and a file that mixes them is refused on read. Each row also carries what
makes it reproducible: the run id, proven, the totals, the totals per part, the assignment, and the identity of what
priced it (identity() below: the capture, its capture shapes and counting versions, reads and writes, the source kinds, the pick
record's and the plan files' commits, the cost cache keys, the code's commit and dirty flag, and `inputs`: one key of
what the search read and was asked, by which a second search-mix of the same question finds its row and searches
nothing: answered()). One writer per file: an upsert holds a lock on it.

Home: output/summary/<dataset>/<scale>/mix-search/<workload>/mixes-of-<settings>[-by-direction].json (home(), stem(),
dataset_scale_dir()). A row's assignment names each part as --mix spells it and each setting in short with its size
({"knows lists": "Ch4k"}: row_assignment, assignment_of), so it pastes into kvcost cost-layouts --mix.
"""
import fcntl
import glob
import hashlib
import json
import os
import subprocess
import re
import sys
import time
import uuid

from kvcost.predict_accuracy.predicted import PREDICTED
from kvevaluator.output_tree import Capture, Tree, capture_in, writes_in
from kvevaluator.paths import ROOT
from kvevaluator import weights

KEY = ("benchmark", "question", "model", "limit", "weighting", "frequencies", "weights", "criterion", "plan_rule", "max_nodes",
       "walk_seconds", "capture", "sizes", "search", "chunk_view")
# the settings a part may take, as the rows' file names group them: per-type (Sp, Co, Ch64k), per-type-and-direction
# (the same, a part per direction), per-type-any-size (Chunked also at each other size)
OPTION_SETS = ("per-type", "per-type-and-direction", "per-type-any-size")
PAPER_SIZES = ("1k", "4k", "16k", "256k")      # the other chunk sizes of the paper's any-size rows
SEARCHES = ("proven", "two-step", "local")     # search-mix --search: branch and bound, the two-step alone, the local search
# the source kinds a capture's counts can be, and the counts each rests on: a composed cell is priced from measured
# parts, so the two may meet in one table. Any other kind (counts predicted from statistics: a capture kvcost predict
# wrote) rests on its own base and never meets measured counts in one table
SOURCE_BASE = {"measured": "measured", "composed": "measured", "predicted": "predicted"}


def dataset_scale_dir(dataset, scale):
    """output/summary/<dataset>/<scale>: where the mix searches of a dataset at a scale write (home)"""
    return str(Tree().summary(dataset, scale))


def setting_rank(short):
    """the order settings are written in: Sp, Co, then Chunked by chunk size"""
    import re as _re
    m = _re.fullmatch(r"Ch(\d+)k", short)
    return (0, 0) if short == "Sp" else (1, 0) if short == "Co" else (2, int(m.group(1)) if m else 0)


def stem(option_set, sizes=None):
    """a rows file's name without .json: mixes-of-<the settings a part may take>[-by-direction], the settings in one order
    whatever order they were given (mixes-of-Sp-Co-Ch64k, mixes-of-Sp-Co-Ch64k-by-direction,
    mixes-of-Sp-Co-Ch1k-Ch4k-Ch16k-Ch64k-Ch256k); sizes: the other chunk sizes ('4k'), default the paper's for any size"""
    if option_set not in OPTION_SETS:
        raise ValueError(f"option set {option_set!r}: one of {OPTION_SETS}")
    other = [] if option_set != "per-type-any-size" else list(PAPER_SIZES if sizes is None else sizes)
    names = sorted({"Sp", "Co", "Ch64k"} | {f"Ch{k}" for k in other}, key=setting_rank)
    return "mixes-of-" + "-".join(names) + ("-by-direction" if option_set == "per-type-and-direction" else "")


def home(base, workload, option_set, suffix="", sizes=None):
    """the result file of one workload (or a question's rows dir) and its settings under a dataset-scale dir
    (dataset_scale_dir): <base>/mix-search/<workload>/<stem>[<suffix>].json (suffix: -local-search, the local search's)"""
    return os.path.join(base, "mix-search", workload, f"{stem(option_set, sizes)}{suffix}.json")


def row_assignment(assign):
    """an assignment as a row keeps it: {part as --mix spells it ('knows lists'): its setting in short ('Ch4k')}, so it
    pastes into kvcost cost-layouts --mix"""
    from kvcost.layout_cost import mixes
    return {mixes.unit_name(u): mixes.setting_short(v) for u, v in sorted(assign.items(), key=lambda kv: kv[0])}


def assignment_of(row_assign):
    """row_assignment's inverse: {unit: setting id} ('knows lists': 'Ch4k' -> ('adj', 'knows'): 'chunked_c4k')"""
    from kvcost.layout_cost import mixes
    return {mixes.unit_of_name(k): mixes.setting_of(v) for k, v in row_assign.items()}


def search_of(row):
    """a row's search method: its own field, else (a row written before it) two-step for a one-node solve, else proven"""
    return row.get("search") or ("two-step" if row.get("max_nodes") == 1 else "proven")


def mix_spec(text, a):
    """the spec file a mix name names, for a command's layout argument: mix:best (the last search-mix's mix of a.workload)
    or mix:<question>/<model>[-<limit>] (the question's fewest row of that model (and byte limit), in the tree --from
    names at the command's dataset and scale; a model the question searches in two option sets, as i01-best's wops and
    bytes, answers with two rows: refused, naming both). The row's spec (export.row_spec) is written to the output dir's
    mix-search/<rows>/mixes/<stem>-<model>[-<limit>].json when missing, and that path returned"""
    from kvcost.cli import runs
    from kvcost.layout_search import export as X
    from kvcost.layout_search import mix_questions as MQ
    name = text[len("mix:"):]
    if name == "best":
        path = runs.best_mix(runs.load(a))
        if not os.path.exists(path):
            raise SystemExit(f"mix:best: no {path} (kvcost search-mix --workload ... writes it)")
        return path
    if "/" not in name:
        raise SystemExit(f"{text}: mix:<question>/<model>[-<limit>] (e.g. mix:i02-any-size/wops_bytes4k) or mix:best")
    qname, want = name.split("/", 1)
    q = MQ.get(qname)
    ds, sc = q["dataset"], getattr(a, "sf", None)
    if getattr(a, "run", None) and q["dataset"] in a.run:
        sc = a.run[q["dataset"]][1 if q.get("larger") and len(a.run[q["dataset"]]) > 1 else 0]
    if sc is None:
        raise SystemExit(f"{text}: give --dataset {ds}=<scale>")
    src = Tree(a.from_dir) if getattr(a, "from_dir", None) else Tree()
    found = []
    for call in q["calls"]:
        flags = call["flags"]
        if "--front" in flags or ("--search" in flags and flags[flags.index("--search") + 1] == "local"):
            continue                         # a front's row holds many mixes; the local search's file is its own
        model = flags[flags.index("--cost-model") + 1] if "--cost-model" in flags else None
        limit = flags[flags.index("--byte-limit") + 1] if "--byte-limit" in flags else None
        if want not in (model, f"{model}-{limit}"):
            continue
        st = stem_of_flags(flags)
        path = home(str(src.summary(ds, sc)), q["rows"], st[0], sizes=st[1])
        search = flags[flags.index("--search") + 1] if "--search" in flags else "proven"   # the question's own search
        rows = [r for r in ResultFile(path).read() if r.get("model") == model and r.get("assignment")
                and (r.get("limit") is None) == (limit is None) and search_of(r) == search]
        found += [(path, st, r, call) for r in rows]
    if len(found) > 1:     # one model searched in two option sets (i01-best: wops per type, and per type and direction)
        named = "; ".join(f"{os.path.relpath(p, src.root)} (search-mix --workload {c['workload']} "
                          f"{' '.join(map(str, c['flags']))})" for p, _st, _r, c in found)
        raise SystemExit(f"{text}: {len(found)} rows of question {qname} answer {want} at {ds} {sc}, one per option "
                         f"set: {named}. A mix name names one row: run the one you want (--dataset {ds}={sc}) and "
                         f"name its mix as mix:best")
    if not found:
        model_of = lambda c: c["flags"][c["flags"].index("--cost-model") + 1] if "--cost-model" in c["flags"] else None
        others = [n for n, x in MQ.questions().items() if n != qname and (x["dataset"], x["rows"]) == (ds, q["rows"])
                  and bool(x.get("larger")) == bool(q.get("larger")) and any(model_of(c) == want for c in x["calls"])]
        raise SystemExit(f"{text}: 0 rows of question {qname} answer {want} at {ds} {sc} in {src.root} "
                         f"(want one; its calls: {', '.join(' '.join(map(str, c['flags'])) for c in q['calls'])})"
                         + (f"; a question of the same rows has a {want} call: mix:{others[0]}/{want}" if others else ""))
    path, st, row, _call = found[0]
    spec, _base = X.row_spec(row)
    out = os.path.join(os.path.dirname(home(str(Tree().summary(ds, sc)), q["rows"], st[0], sizes=st[1])), "mixes",
                       f"{stem(*st)}-{want}.json")
    if not os.path.exists(out) or json.load(open(out)) != spec:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump(spec, f, indent=2, sort_keys=True)
            f.write("\n")
    return out


def stem_of_flags(flags):
    """(option set, other sizes) of search-mix flags: --settings and --by-direction"""
    from kvcost.cli.search import settings_option_set
    settings = flags[flags.index("--settings") + 1] if "--settings" in flags else "Sp,Co,Ch64k"
    return settings_option_set(settings, "--by-direction" in flags)


def row_key(row):
    """a row's KEY values; a row written before "sizes", "weights" or "chunk_view" were key fields has none: no
    sizes, the default op-weight vector, the measured chunk view (every row before the last-chunk lane,
    costed a write's last chunk as measured); its capture and sizes' paths as paths.recorded_path writes
    them (an older row's absolute path keys as the same row)"""
    default = {"sizes": [], "weights": weights.text(weights.DEFAULT), "chunk_view": "measured"}
    from kvevaluator.output_tree import Tree
    from kvevaluator.paths import recorded_path
    rec = lambda p: recorded_path(p, Tree().root) if isinstance(p, str) else p   # a row before recorded paths: absolute
    row = dict(row, capture=rec(row.get("capture")),
               sizes=[[s[0], rec(s[1]), *s[2:]] for s in row.get("sizes") or []])
    return tuple(json.dumps((row.get(k) or default[k]) if k in default else rule_key(row.get(k)) if k == "plan_rule"
                            else search_of(row) if k == "search" else row.get(k), sort_keys=True) for k in KEY)


def frequencies(items):
    """a workload's frequencies as a row keys them: the sha256 of its items (family, query, frequency, params), sorted"""
    text = json.dumps(sorted([list(i) for i in items], key=json.dumps), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


_LEGACY_SAID = set()


def one_frequencies(path, rows, row=None):
    """refuse rows of one file (and the row to add) at more than one set of frequencies; a row written before the field
    has none and matches any, said once per file"""
    have = {r["frequencies"]: r.get("workload_file") for r in rows if r.get("frequencies")}
    if row is not None and row.get("frequencies") and have and row["frequencies"] not in have:
        h, wf = next(iter(have.items()))
        raise SystemExit(f"{path}: holds rows at frequencies {h[:12]} ({wf}); this run's are {row['frequencies'][:12]} "
                         f"({row.get('workload_file')}): give another --out -- REFUSED")
    if len(have) > 1:
        raise SystemExit(f"{path}: rows at {len(have)} sets of frequencies ({', '.join(sorted(map(str, have.values())))}): "
                         "one file holds one -- REFUSED")
    legacy = sum(1 for r in rows if not r.get("frequencies"))
    if legacy and path not in _LEGACY_SAID:
        _LEGACY_SAID.add(path)
        print(f"[results] {path}: {legacy} rows written before the frequencies field: taken as this file's "
              "frequencies, not checked", file=sys.stderr)


class ResultFile:
    """one result file: read(), upsert(row) (the one writer: a lock on <path>.lock)"""

    def __init__(self, path):
        self.path = path

    def read(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            rows = json.load(f)["rows"]
        one_frequencies(self.path, rows)
        return rows

    def upsert(self, row):
        """add the row, or replace the row with the same key; rows kept sorted by key (the file is deterministic)"""
        missing = [k for k in KEY if k not in row]
        if missing:
            raise ValueError(f"a result row needs {missing}")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            old = self.read()
            one_frequencies(self.path, old, row)
            rows = {row_key(r): r for r in old}
            replaced = row_key(row) in rows
            rows[row_key(row)] = row
            tmp = f"{self.path}.tmp{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump({"rows": [rows[k] for k in sorted(rows)]}, f, indent=1, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)
        return replaced


def _in_repo(path):
    """a path as the repo names it (relative to the repo root), so a row is the same from any clone or directory"""
    return os.path.relpath(os.path.abspath(path), ROOT)


def workload_file(workload):
    """a search's workload as its result records it: a workload file as the repo names it (_in_repo), an id as given"""
    return _in_repo(workload) if workload.endswith(".json") else workload


RULE_FIELDS = ("record", "anchor", "pin", "in_sample", "historical")   # the plan rule's fields that change a result


def plan_rule_text(rule):
    """the plan rule as a row keys it: the fields that change a result, by name (not its repr: the in-sample default
    tuple is code, and a row must keep its key when the code moves), the pick record as a path in the repo"""
    record = _in_repo(rule.record) if rule.record else None
    return "; ".join(f"{k}={record if k == 'record' else getattr(rule, k)}" for k in RULE_FIELDS) + (
        "" if rule.held_out else "; held_out=False")      # named only when the held-out rows are not read


_OLD_RULE = re.compile(r"(\w+)=('[^']*'|\([^)]*\)|[^,)]+)")
_OLD_SAID = []


def rule_key(text):
    """a row's plan rule as the key compares it: a row may hold the rule's repr
    ("PlanRule(record='...', anchor=True, ...)"), read here in the fields form (plan_rule_text), said once per process"""
    if not isinstance(text, str) or not text.startswith("PlanRule("):
        return text
    got = {k: v.strip("'") for k, v in _OLD_RULE.findall(text[len("PlanRule("):-1])}
    if not _OLD_SAID:
        _OLD_SAID.append(1)
        print("[results] rows that name the plan rule by its repr: keyed by its fields "
              "(results.rule_key)", file=sys.stderr)
    return "; ".join(f"{k}={got.get(k)}" for k in RULE_FIELDS)


def run_id():
    """a run's id: the UTC time and a short random part"""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]


def _git(*args):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def code_state():
    """(commit, dirty) of the code that priced: the tree's HEAD and whether python/, experiments/ or inputs/ differ"""
    sha = _git("rev-parse", "HEAD")
    if sha is None:
        return None, None
    return sha, bool(_git("status", "--porcelain", "--", "python", "experiments", "inputs"))


def predicted_capture(root):
    """the capture a root names (Capture.at) when its records say measured_as predicted, else None: a predicted
    capture is priced only under its own plan rule (search-mix --count-source <set>), never as measured counts"""
    try:
        cap = Capture(root)
    except (SystemExit, Exception):     # not a capture this check can name: the composer says why
        return None
    d = cap.dir("counts")
    rec = next((f for f in sorted(os.listdir(d)) if f.endswith(".json")), None) if os.path.isdir(d) else None
    if rec and json.load(open(os.path.join(d, rec))).get("measured_as") == PREDICTED:
        return cap.path
    return None


def source_kind(read_dir, cell):
    """a cell's counts as a capture holds them: predicted (its record says so: kvevaluator predict --plans), composed
    (the composer left its marks, kind exact) or measured"""
    cap = Capture(read_dir)
    rec = cap.record(cell)
    if os.path.exists(rec) and json.load(open(rec)).get("measured_as") == PREDICTED:
        return "predicted"
    return "composed" if os.path.exists(cap.path_of("exact", cell)) else "measured"


def source_kinds(data):
    """{family: {cell: kind}} of each cell a load read, from the capture it read it from (output_tree.layout_capture)"""
    return {str(fam): {c: source_kind(rd, c) for c, rd in sorted(dirs.items())}
            for fam, dirs in sorted(data.read_dirs.items())}


def check_sources(kinds):
    """the set of source kinds; refuses counts resting on different bases (measured and predicted)"""
    seen = sorted({k for per in kinds.values() for k in per.values()})
    bases = sorted({SOURCE_BASE.get(k, k) for k in seen})
    if len(bases) > 1:
        raise SystemExit(f"the cost cache would mix counts of different sources ({', '.join(seen)}): refused")
    return seen


def _versions(counts_dir, key="measurement_version"):
    """the sorted distinct values of `key` over the run records in counts_dir (a dict value as its sorted JSON text)"""
    vs = set()
    for p in glob.glob(os.path.join(counts_dir, "*.json")):
        with open(p) as f:
            v = json.load(f).get(key)
        vs.add(json.dumps(v, sort_keys=True) if isinstance(v, dict) else v)
    return sorted(vs, key=str)


def measurement_versions(data, capture, dataset, key="measurement_version"):
    """{family: the sorted values of `key` in the run records of its capture dir}, the read families' and the write
    families' (the writes capture under the same root): the capture shapes ("measurement_version") or the counting
    versions they were counted under ("counting"; none on a record without that field)"""
    out = {str(fam): sorted({v for rd in sorted(set(dirs.values())) for v in _versions(Capture(rd).dir("counts"), key)},
                            key=str) for fam, dirs in sorted(data.read_dirs.items())}
    for fam, kind in sorted(data.families.items()):
        if kind == "write":
            out[str(fam)] = _versions(writes_in(capture, dataset, data.composer.param_set).dir("counts"), key)
    for oid, sroot, _cell in data.composer.sizes:          # the other chunk sizes' captures, reads and writes
        for fam, kind in sorted(data.families.items()):
            cap = (writes_in(sroot, dataset, data.composer.param_set) if kind == "write" else
                   capture_in(sroot, fam, data.composer.param_set))
            out[f"{oid}/{fam}"] = _versions(cap.dir("counts"), key)
    return out


def identity(data, capture, cost_cache_keys, dataset, inputs):
    """what a row was priced from: capture, versions, source kinds, the pick record and its last commit, the plan files'
    last commit at the code's HEAD (the plans the capture ran are in its run records), the cost cache keys, the code's
    state; and `inputs`, the key of what the search read and was asked (search-mix reuses the row while it holds: answered)"""
    commit, dirty = code_state()
    rule = data.composer.plan_rule
    has = rule.record is not None and os.path.exists(rule.record)
    from kvevaluator.output_tree import Tree
    from kvevaluator.paths import recorded_path
    return {"capture": recorded_path(os.path.abspath(capture), Tree().root), "measurement_version": measurement_versions(data, capture, dataset),
            "counting": measurement_versions(data, capture, dataset, "counting"),
            "source_kinds": source_kinds(data), "pick_record": _in_repo(rule.record) if rule.record else None,
            "pick_record_commit": (_git("log", "-1", "--format=%H", "--", rule.record) or None) if has else None,
            "plan_files_commit": _git("log", "-1", "--format=%H", "--", "python/kvevaluator/workloads"),
            "cost_cache_keys": sorted(cost_cache_keys), "code_commit": commit, "code_dirty": dirty, "inputs": inputs}


def answered(path, inputs):
    """the row of the result file `path` searched on `inputs` (identity's key), or None"""
    return next((r for r in ResultFile(path).read() if (r.get("identity") or {}).get("inputs") == inputs), None)
