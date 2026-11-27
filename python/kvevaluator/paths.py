"""Where things are: the repo root (from this file) and the four locations (from the environment or the root's .env).

The root is never configured: it is two levels above this file; INPUTS_DIR (<root>/inputs: params, specs, picks) is
part of the checkout. The locations are the only knobs:

    KVEVAL_DATA_DIR     the datasets and fetched results     (default <root>/data)
    KVEVAL_OUTPUT_DIR   stores, captures, figures            (default <root>/output)
    KVEVAL_BUILD_DIR    the C++ build                        (default <root>/build)
    KVEVAL_CPP_BIN      the kvevaluator binary               (default <build>/cpp/kvevaluator)

The data dir holds one dir and nothing else (anything beside it is refused, by name):

    <data>/datasets/   the input datasets: ldbc/ (`just fetch-dataset`), imdb/              datasets_dir()

The paper's runs are one output tree at <root>/paper-results (`just fetch-results`; paper_results_dir()): read, never
written (refuse_results, output_twin).

Sub-locations, each a run's own dir below a location (default in brackets), read only by the functions below:

    KVEVAL_STORE_DIR           the C++ stores                  [<output>/stores]
    KVEVAL_STATS_DIR           the statistics, record streams, tally library and mix cost cache  [<output>/stats]
    KVEVAL_SPECS_DIR           the specs made from a layout's name  [<output>/specs]
    KVEVAL_IMDB_DIR            JOB's IMDb dataset              [<data>/datasets/imdb]

The environment wins over .env. .env holds KEY=VALUE lines, read as text (no shell: no $VAR, no quotes beyond one
pair around the value). A relative value is taken from the root. A location inside another git checkout is refused,
so a run never writes into some other repo; the one exception is the build cache this repo's checkouts share.

The build cache (`just build`, run/build_cpp.py) is not a knob: one per machine, in the build dir of the clone every
worktree of this repo belongs to (<main clone>/build/cache), found from git                  build_cache_dir()

    python -m kvevaluator.paths              # print every location
    python -m kvevaluator.paths build_dir    # print one (for the justfile and shell drivers); also datasets_dir,
                                             # paper_results_dir, build_cache_dir
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # python/kvevaluator/paths.py -> the repo root
INPUTS_DIR = ROOT / "inputs"                        # tracked inputs: <kind>/<dataset>/[<scale>/...] (inputs/README.md)
CONTRACT_DIR = INPUTS_DIR / "contract"              # the facts C++ and Python share (kvevaluator.contract)

KEYS = ("KVEVAL_DATA_DIR", "KVEVAL_OUTPUT_DIR", "KVEVAL_BUILD_DIR", "KVEVAL_CPP_BIN")
SUB_KEYS = ("KVEVAL_STORE_DIR", "KVEVAL_STATS_DIR", "KVEVAL_SPECS_DIR", "KVEVAL_IMDB_DIR")


class PathsError(Exception):
    """A bad .env line, an unknown key, or a location inside another checkout."""


def read_env_file(path):
    """The KEY=VALUE pairs of a .env file, parsed as text; a bad line or an unknown key is an error."""
    values = {}
    if not path.is_file():
        return values
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise PathsError(f"{path}:{n}: not a KEY=VALUE line: {line!r}")
        if key not in KEYS + SUB_KEYS:
            raise PathsError(f"{path}:{n}: unknown key {key} (known: {', '.join(KEYS + SUB_KEYS)})")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if "$" in value or value.startswith("~"):
            raise PathsError(f"{path}:{n}: {key} is read without a shell; write the path out, not {value!r}")
        values[key] = value
    return values


def checkout_of(path):
    """The nearest enclosing dir that holds a .git, or None (the path need not exist yet)."""
    for d in (path, *path.parents):
        if (d / ".git").exists():
            return d
    return None


def release_commit(root=None):
    """The commit a release archive was cut from (RELEASE_COMMIT, filled in by git archive's export-subst), or None: in a
    git checkout (the file still holds its placeholder) and in a copy that was not made by git archive"""
    import re
    root = Path(root) if root is not None else ROOT
    if checkout_of(root) is not None:
        return None
    try:
        text = (root / "RELEASE_COMMIT").read_text().strip()
    except FileNotFoundError:
        return None
    return text if re.fullmatch(r"[0-9a-f]{40}", text) else None


def build_cache_dir():
    """The C++ build cache: <main clone>/build/cache, where the main clone is the one whose .git every worktree of
    this repo shares (`git rev-parse --git-common-dir`); None when the root is not in a git checkout (a tarball:
    `just build` then builds in place)."""
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True)
    common = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    if common is None or common.name != ".git":   # no git, or a bare repo: no clone to hold the cache
        return None
    return common.parent / "build" / "cache"


def gate_records_dir():
    """`just gate full`'s pass records: <main clone>/build/gate-records, beside the build cache (one per clone, shared by
    its worktrees, never tracked). The merge check, outside this repo, finds it the same way (a second copy of this rule)."""
    cache = build_cache_dir()
    return None if cache is None else cache.parent / "gate-records"


def _in_build_cache(p):
    """p is in the build cache (a binary or a control that `just build` made, shared by every worktree): allowed for
    KVEVAL_CPP_BIN only, never as a data, output or build dir"""
    cache = build_cache_dir()
    return cache is not None and (p == cache or cache in p.parents)


def _path(key, default, environ, from_file):
    """The location `key` names (environment, then .env), else `default`: absolute, never inside another checkout."""
    value = environ.get(key) or from_file.get(key)
    if not value and default is None:
        return None
    p = Path(value) if value else Path(default)
    p = (p if p.is_absolute() else ROOT / p).resolve()
    owner = checkout_of(p)
    if owner is not None and owner != ROOT and not (key == "KVEVAL_CPP_BIN" and _in_build_cache(p)):
        raise PathsError(f"{key}={p} is inside another git checkout ({owner}); use a dir outside it")
    return p


def resolve(environ=None, env_file=None):
    """The four locations as absolute Paths, keyed data_dir, output_dir, build_dir, cpp_bin."""
    environ = os.environ if environ is None else environ
    from_file = read_env_file(ROOT / ".env" if env_file is None else env_file)

    def get(key, default):
        return _path(key, default, environ, from_file)

    build_dir = get("KVEVAL_BUILD_DIR", ROOT / "build")
    return {
        "data_dir": get("KVEVAL_DATA_DIR", ROOT / "data"),
        "output_dir": get("KVEVAL_OUTPUT_DIR", ROOT / "output"),
        "build_dir": build_dir,
        "cpp_bin": get("KVEVAL_CPP_BIN", build_dir / "cpp" / "kvevaluator"),
    }


_locations = resolve()
DATA_DIR = _locations["data_dir"]
OUTPUT_DIR = _locations["output_dir"]
BUILD_DIR = _locations["build_dir"]
CPP_BIN = _locations["cpp_bin"]
_FROM_FILE = read_env_file(ROOT / ".env")


# The functions below read the environment when called, not at import: a test may point a child, or this
# process, at another binary or a scratch dir after the import.
def cpp_bin():
    """The kvevaluator binary the environment names now (KVEVAL_CPP_BIN), else the build dir's."""
    build = _path("KVEVAL_BUILD_DIR", ROOT / "build", os.environ, _FROM_FILE)
    return _path("KVEVAL_CPP_BIN", build / "cpp" / "kvevaluator", os.environ, _FROM_FILE)


def store_dir():
    """The root of the C++ stores: KVEVAL_STORE_DIR, else <output>/stores (kvevaluator.output_tree)."""
    return _path("KVEVAL_STORE_DIR", _path("KVEVAL_OUTPUT_DIR", ROOT / "output", os.environ, _FROM_FILE) / "stores",
                 os.environ, _FROM_FILE)


def stats_dir():
    """The root of the shared statistics (the record streams, stats.json, the tally library, the mix cost cache):
    KVEVAL_STATS_DIR, else <output>/stats (kvevaluator.output_tree.stats_dir). A tree written elsewhere
    (KVEVAL_OUTPUT_DIR=<dir>, output_tree.own_tree) reads the shared ones through it."""
    return _path("KVEVAL_STATS_DIR", _path("KVEVAL_OUTPUT_DIR", ROOT / "output", os.environ, _FROM_FILE) / "stats",
                 os.environ, _FROM_FILE)


def specs_dir():
    """The specs made from a layout's name that no spec set in inputs/specs holds (kvevaluator.layouts): KVEVAL_SPECS_DIR,
    else <output>/specs, named by what they hold as the stores are. A tree written elsewhere (KVEVAL_OUTPUT_DIR=<dir>,
    output_tree.own_tree) reads and writes the shared ones through it."""
    return _path("KVEVAL_SPECS_DIR", _path("KVEVAL_OUTPUT_DIR", ROOT / "output", os.environ, _FROM_FILE) / "specs",
                 os.environ, _FROM_FILE)


DATA_LAYOUT = ("datasets",)   # the data dir's only entry


def _data_part(name):
    """<data dir>/<name>, after checking the data dir holds nothing but DATA_LAYOUT: an entry beside it is refused,
    never read through a fallback"""
    data = _path("KVEVAL_DATA_DIR", ROOT / "data", os.environ, _FROM_FILE)
    extra = sorted(p.name for p in data.iterdir() if p.name not in DATA_LAYOUT) if data.is_dir() else []
    if extra:
        raise PathsError(f"the data dir {data} holds {len(extra)} entries beside datasets/ ({', '.join(extra)}): move "
                         f"the input datasets (ldbc/, imdb/) under datasets/, anything else out of the data dir")
    return data / name


def datasets_dir():
    """The input datasets: <data dir>/datasets (ldbc/, imdb/)"""
    return _data_part("datasets")


def paper_results_dir():
    """The paper's results: one output tree (python/kvevaluator/output_tree.py) at <root>/paper-results, beside output/"""
    return ROOT / "paper-results"


def _under_results(path):
    """path's place relative to paper_results_dir() (real paths, so a linked paper-results is caught too), else None"""
    res, p = os.path.realpath(paper_results_dir()), os.path.realpath(path)
    return os.path.relpath(p, res) if p == res or p.startswith(res + os.sep) else None


ELSEWHERE = "<elsewhere>"     # a recorded path that was on no tree or checkout a reader has (another machine's disk)
# the dir names a machine path is cut at (recorded_path): an output tree's kinds, a checkout's top dirs, the store root
_PLACES = ("captures", "composed", "predicted", "summary", "timings", "stats", "stores", "specs", "engines", "studies",
           "figures", "paper", "snapshots", "output", "build", "inputs", "data", "experiments", "rocksdb-cpp")


def recorded_path(path, tree=None):
    """a path as a results file records it, naming no machine: relative to the output tree `tree` when inside it, else
    relative to the repo root when inside it, else ELSEWHERE/ and the path from its first place dir (_PLACES, after the
    disk's first two dirs: its mount point and its disk or home), else ELSEWHERE/ and its last part (the dirs above it
    are one machine's).
    A relative path is kept as given (it names no machine). resolve_recorded reads it."""
    if not os.path.isabs(path):
        return str(path)
    p = os.path.abspath(path)
    for root in (tree, ROOT):
        r = None if root is None else os.path.abspath(root)
        if r is not None and (p == r or p.startswith(r.rstrip(os.sep) + os.sep)):
            return os.path.relpath(p, r)
    parts = p.split(os.sep)[3:]
    cut = next((i for i, d in enumerate(parts) if d in _PLACES), max(len(parts) - 1, 0))
    return "/".join([ELSEWHERE, *parts[cut:]])


def resolve_recorded(path, tree=None):
    """the file a recorded path names (recorded_path): absolute as given; else under the tree `tree` when there, else
    under the repo root (where an older record's paths are relative to)"""
    if os.path.isabs(path):
        return path
    for root in (tree, ROOT):
        if root is not None and os.path.exists(os.path.join(root, path)):
            return os.path.join(root, path)
    return os.path.join(tree if tree is not None else ROOT, path)


def output_twin(path):
    """Where a script writes what belongs beside `path`: `path` itself, or for a path under paper-results/ the same place
    under OUTPUT_DIR (the paper's runs are read, never written into: their files are pinned)."""
    rel = _under_results(path)
    return str(path) if rel is None else os.path.normpath(os.path.join(str(OUTPUT_DIR), rel))


def refuse_results(path):
    """`path` (an --out a caller gave), refused when it is under paper-results/: the paper's runs are never written into."""
    if _under_results(path) is not None:
        sys.exit(f"refused: {path} is under the paper's runs ({paper_results_dir()}), which are read, never written; "
                 f"give an --out under the output dir ({OUTPUT_DIR})")
    return str(path)


def figure_dir(default, out=None):
    """A drawing script's output dir: its --out (refused under paper-results/), else `default` (moved under the
    output dir when it is inside paper-results/)."""
    return refuse_results(out) if out else output_twin(default)


def imdb_dir():
    """JOB's IMDb dataset: KVEVAL_IMDB_DIR, else <data>/datasets/imdb."""
    if os.environ.get("KVEVAL_IMDB_DIR") or _FROM_FILE.get("KVEVAL_IMDB_DIR"):
        return _path("KVEVAL_IMDB_DIR", None, os.environ, _FROM_FILE)
    return datasets_dir() / "imdb"


def ldbc_dir(sf, variant=None):
    """One SF's LDBC SNB CSVs: <data dir>/datasets/ldbc/sf<SF>[-<variant>]/ (`just fetch-dataset ldbc=<sf>`); a variant is a
    derived copy"""
    return datasets_dir() / "ldbc" / (f"sf{sf}" + ("" if variant is None else f"-{variant}"))


def ldbc_holdout_file():
    """engine fidelity's LDBC holdout record (the held-out params, the freezes, the seen pairs): tracked input"""
    return INPUTS_DIR / "params" / "ldbc" / "ldbc_holdout.json"


def ldbc_params_dir(sf):
    """LDBC's hosted substitution parameters for one SF: <data dir>/datasets/ldbc/substitution_parameters-sf<SF>/"""
    return datasets_dir() / "ldbc" / f"substitution_parameters-sf{sf}"


PARTS = {"datasets_dir": datasets_dir, "store_dir": store_dir, "stats_dir": stats_dir, "specs_dir": specs_dir,   # printed beside the four locations
         "build_cache_dir": build_cache_dir, "gate_records_dir": gate_records_dir, "paper_results_dir": paper_results_dir}


def main(argv):
    names = [*_locations, *PARTS]
    if len(argv) > 1 or (argv and argv[0] not in names):
        sys.exit(f"usage: python -m kvevaluator.paths [{'|'.join(names)}]")
    try:
        if argv:
            print(_locations[argv[0]] if argv[0] in _locations else PARTS[argv[0]]())
        else:
            print(f"root\t{ROOT}")
            for name, p in _locations.items():
                print(f"{name}\t{p}")
            for name, part in PARTS.items():
                print(f"{name}\t{part()}")
    except PathsError as e:
        sys.exit(f"kvevaluator.paths: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
