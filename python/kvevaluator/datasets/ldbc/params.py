"""LDBC substitution parameters: ONE place for the param-file conventions, shared by every driver, so evaluator runs
and the live-engine measurements always join on identical params.

IC1-12 read interactive_<N>_param.txt, LDBC's hosted file or ours, as inputs/params/ldbc/param_sources.json says
(param_file). IS1-3 are person-anchored and reuse IC1's personId column; IS4-7 read the tracked short-param files
(messageId|kind|depth; LDBC datagen ships no short-read params)."""
import json
import os
import re

from kvevaluator.paths import INPUTS_DIR, ROOT, ldbc_params_dir
from kvevaluator.datasets import params_in

# The tracked LDBC params (the data dir is not tracked, so this is the portable source of truth): per SF, verbatim
# copies of the hosted files (sf<SF>/interactive/interactive_<N>_param.txt, sha256 in hosted_params.meta.json), ours
# beside them (sf<SF>/interactive/ours/, each with a meta.json saying how it was made), the IS4-7 anchors
# (sf<SF>/interactive/short/), and param_sources.json, which says which one each (SF, query) reads. An SF with no tracked copy reads the hosted download
# (ldbc_params_dir).
_LDBC_OVERRIDE = str(INPUTS_DIR / "params" / "ldbc")                   # the scale-free files: param_sources.json, ...
SOURCES_FILE = os.path.join(_LDBC_OVERRIDE, "param_sources.json")


def _interactive(sf):
    """LDBC Interactive's tracked params at a scale (paths.params_in)"""
    return str(params_in("ldbc", sf, "interactive"))


#  A query that borrows another's param rows declares it: `params_like` in its plan file (a query someone adds
#  declares it in its Python, Plan(..., params_like="ic1"), and generate-plans writes it there), else
#  in the workload's manifest (python/kvevaluator/workloads/interactive/manifest.json: IS1-3 and the person-anchored
#  wide reads take IC1's person ids; the reply thread is7's reply-STRATIFIED message ids, so its reply set is
#  non-empty). Its args are its declared params, each from its row column (ROW_COLUMN).
#  The forum board has its own generated forum anchors (forums that have posts).
_PLANS = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "interactive", "plans")
_MANIFEST = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "interactive", "manifest.json")
ROW_COLUMN = {"person": "personId", "message": "messageId", "forum": "forumId", "maxDate": "maxDate",
              "person1": "person1Id", "person2": "person2Id"}          # two people: params_like "ic13"
_DECLARED = {}


def declared(qname):
    """(params_like, the declared param names) of a query: its plan file's, else the manifest's params_like"""
    if qname not in _DECLARED:
        path = os.path.join(_PLANS, f"{qname}.json")
        ir = json.load(open(path)) if os.path.exists(path) else {}
        like = ir.get("params_like") or json.load(open(_MANIFEST)).get("params_like", {}).get(qname)
        _DECLARED[qname] = (like, list(ir.get("params", {})))
    return _DECLARED[qname]


def _forum_param_rows(sf):
    """Forum anchors for forum_board_render: forums that HAVE posts, most-posts-first (gen_forum_params.py), so the wide
    read always projects. The tracked dir first, the hosted dir as the fallback.
    """
    override = os.path.join(_interactive(sf), "forum_param.txt")
    hosted = os.path.join(ldbc_params_dir(sf), "forum_param.txt")
    path = override if os.path.exists(override) else hosted
    with open(path) as f:
        header = f.readline().rstrip("\n").split("|")
        return [dict(zip(header, line.rstrip("\n").split("|"))) for line in f]


def param_sources():
    """param_sources.json: {"default": ..., "pairs": {"sf<SF>": {"ic<N>": {"source": "hosted" | "ours", ...}}}}"""
    with open(SOURCES_FILE) as f:
        return json.load(f)


def param_source(sf, ic):
    """"hosted" or "ours" for one (SF, IC query): its row in param_sources.json, else the default"""
    t = param_sources()
    src = t["pairs"].get(f"sf{sf}", {}).get(ic, {}).get("source", t["default"])
    if src not in ("hosted", "ours"):
        raise SystemExit(f"{SOURCES_FILE}: sf{sf} {ic}: source {src!r} is not hosted or ours")
    return src


UNIFORM = "uniform"
# The param sets LDBC Interactive's queries can read, the first the default: "ldbc" (LDBC's files, as
# param_sources.json says) and "uniform" (the same rows with the bound person or message drawn uniformly at random:
# uniform_dir, written by workloads/interactive/gen_uniform_params.py). A set is an identity axis of a capture: a
# writer takes it by flag (--param-set) and a non-default set gets its own <params> dir (output_tree.params_dir), so a
# reader finds the set in the path it reads. None everywhere below = the default set.
PARAM_SETS = ("ldbc", UNIFORM)


def check_param_set(param_set):
    """a param set as code passes it: None for the default set ("ldbc" given or nothing), else its name; another name
    is refused. The LDBC_PARAMS variable (the old way to choose a set) is refused while set: left in a shell, it
    would otherwise run LDBC's params with no word"""
    if os.environ.get("LDBC_PARAMS"):
        raise SystemExit(f"LDBC_PARAMS={os.environ['LDBC_PARAMS']} is set, and no code reads it any more: unset it, and "
                         f"pass the set as --param-set ({', '.join(PARAM_SETS)}) -- REFUSED")
    if param_set in (None, PARAM_SETS[0]):
        return None
    if param_set not in PARAM_SETS:
        raise SystemExit(f"param set {param_set!r}: not one of LDBC's ({', '.join(PARAM_SETS)}) -- REFUSED")
    return param_set


def uniform_dir(sf):
    """the uniform param set's files at a scale: the same names as LDBC's (IC and short-read files side by side)"""
    return os.path.join(_interactive(sf), UNIFORM)


def param_file(sf, qname, source=None, param_set=None):
    """THE file a query's params come from (param_rows reads it; kvstats_backfill keys its reuse on it): the source
    inputs/params/ldbc/param_sources.json names, inputs/params/ldbc/README.md says why. `source`
    forces "hosted" or "ours" for an IC file (the census reads hosted rows of a pair that uses ours); None = the
    table's choice. `param_set`: PARAM_SETS (None: the default, LDBC's); the uniform set's file has the same name in
    uniform_dir. None for the forum anchors (_forum_param_rows reads them)."""
    path = _ldbc_param_file(sf, qname, source)
    if source is None and check_param_set(param_set) == UNIFORM:
        if path is None:
            raise SystemExit(f"{qname}: the uniform param set has no forum anchors (it binds persons and messages "
                             f"only) -- REFUSED")
        path = os.path.join(uniform_dir(sf), os.path.basename(path))
        if not os.path.exists(path):
            raise SystemExit(f"param set uniform: {path} does not exist; make it: python -m "
                             f"kvevaluator.workloads.interactive.gen_uniform_params --sf {sf}")
    return path


def _ldbc_param_file(sf, qname, source=None):
    """LDBC's file for a query (param_file's, before the uniform set)"""
    like = declared(qname)[0]
    if like:                                         # a declared borrow (IS1-3, the wide reads, ...)
        return _ldbc_param_file(sf, like, source)
    if qname == "forum_board_render":
        return None
    m = re.fullmatch(r"(ic|is)(\d+)(_\w+)?", qname)
    if not m:
        raise SystemExit(f"{qname}: no params source: declare the query whose param rows it runs on, \"params_like\": "
                         f"\"ic1\" (IC1's person ids; each declared param is read from that row's column: "
                         f"{', '.join(f'{k} <- {v}' for k, v in ROW_COLUMN.items())}): in the query's Python, "
                         f"Plan(..., params_like=\"ic1\"), then kvcost generate-plans writes it into its plan file "
                         f"{os.path.join(_PLANS, qname + '.json')}")
    kind, num = m.group(1), int(m.group(2))   # a _suffix twin shares its base's rows
    if kind == "is" and num >= 4:
        return os.path.join(_interactive(sf), "short", f"interactive_short_param_{num}.txt")
    n = 1 if kind == "is" else num          # IS1-3 reuse IC1's personId column
    src = source or param_source(sf, f"ic{n}")
    name = f"interactive_{n}_param.txt"
    if src == "ours":
        path = os.path.join(_interactive(sf), "ours", name)
        if not os.path.exists(path):
            raise SystemExit(f"sf{sf} ic{n}: param_sources.json says ours, but {path} does not exist "
                             f"(a record of another params set; not shipped for this SF)")
        return path
    copy = os.path.join(_interactive(sf), name)        # the tracked verbatim copy, else the download
    return copy if os.path.exists(copy) else os.path.join(ldbc_params_dir(sf), name)


def param_rows(sf, qname, source=None, param_set=None):
    """All raw param rows for a query (list of dicts keyed by the file header). `source`, `param_set`: see param_file."""
    if qname == "forum_board_render":
        if source is None and check_param_set(param_set) is not None:
            param_file(sf, qname, param_set=param_set)     # refuses: the uniform set has no forum anchors
        return _forum_param_rows(sf)
    path = param_file(sf, qname, source, param_set)
    with open(path) as f:
        header = f.readline().rstrip("\n").split("|")
        return [dict(zip(header, line.rstrip("\n").split("|"))) for line in f]


def args_from_row(qname, r):
    """The query-builder args for one raw param row."""
    like, names = declared(qname)
    if like and not qname.startswith("is"):          # a declared borrow: its params, each from its row column
        unknown = [p for p in names if p not in ROW_COLUMN]
        if unknown:
            raise SystemExit(f"{qname}: params {unknown} have no row column (known: "
                             f"{', '.join(f'{k} <- {v}' for k, v in ROW_COLUMN.items())}): add one to ROW_COLUMN in "
                             f"{os.path.relpath(__file__, str(ROOT))}")
        missing = [ROW_COLUMN[p] for p in names if ROW_COLUMN[p] not in r]
        if missing:
            raise SystemExit(f"{qname}: params_like {like!r}: its rows have no column {missing} (they have "
                             f"{sorted(r)}): pick a query whose param file has them")
        return {p: r[ROW_COLUMN[p]] for p in names}
    if qname == "forum_board_render":                # forum-anchored
        return {"forum": r["forumId"]}
    if qname.startswith("is"):
        return {"message": r["messageId"]} if "messageId" in r else {"person": r["personId"]}
    qname = qname.split("_")[0]                     # a _suffix twin shares its base's args
    extra = {
        "ic1": lambda: {"firstName": r["firstName"]},
        "ic2": lambda: {"maxDate": r["maxDate"]},
        "ic3": lambda: {"startDate": r["startDate"], "durationDays": r["durationDays"],
                        "countryXName": r["countryXName"], "countryYName": r["countryYName"]},
        "ic4": lambda: {"startDate": r["startDate"], "durationDays": r["durationDays"]},
        "ic5": lambda: {"minDate": r["minDate"]},
        "ic6": lambda: {"tagName": r["tagName"]},
        "ic7": lambda: {},
        "ic8": lambda: {},
        "ic9": lambda: {"maxDate": r["maxDate"]},
        "ic10": lambda: {"month": r["month"]},
        "ic11": lambda: {"countryName": r["countryName"], "year": r["workFromYear"]},
        "ic12": lambda: {"tagClassName": r["tagClassName"]},
    }[qname]()
    return {"person": r["personId"], **extra}


def query_params(sf, qname, count=None, source=None, param_set=None):
    """Arg dicts for the first `count` params of a query (all when count=None). `source`, `param_set`: see param_file."""
    rows = param_rows(sf, qname, source, param_set)
    if count:
        rows = rows[:count]
    return [args_from_row(qname, r) for r in rows]
