"""The datasets: one folder each (datasets/<name>/), described by its manifest.json; this module is the one table of them.

A dataset is the graph a workload runs on: its schema, its scale axis (`sf` for LDBC, a data tag for LinkBench and
IMDb) and the dir a scale is named by (`scale_dir`), its dir name in the output tree (`output_root`),
the module its record stream is dumped by (`records_source`: every store is ingested from that stream), the module that
fetches or makes its data at a scale (`fetch_module`: `just fetch-dataset`, kvevaluator.fetch_dataset), and its pricer
name (`benchmark`). Its families are the workloads whose `dataset` it is (families()).

    get("ldbc")          -> Dataset
    schema("ldbc")       -> its compiled canonical schema
    schema_path("ldbc")  -> the absolute path of its compiled schema JSON (`schema_<design>.json` for another design);
                            the one way code finds that file (shell: python -m kvevaluator.datasets schema-path ldbc)
    names()              -> ["ldbc", "linkbench", "imdb"], in manifest order
    of_suite("job")      -> the Dataset whose pricer suite is "job" (IMDb)
    fetch_hint("ldbc", "3") -> "just fetch-dataset ldbc=3": the one command that gets a dataset's data, for a message
"""
import glob
import importlib
import json
import os
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class Dataset:
    """one dataset, as its manifest.json declares it"""
    name: str
    order: int
    title: str
    scale_axis: str                 # "sf" (the LDBC scale factor) or "tag" (a data tag: n1m, sample1pct, full)
    scales: list                    # the scales it has, smallest first (its data, params and workloads exist at each)
    deciding_scale: object          # the scale its pick record is decided at (LDBC: "3"); None = no record
    schema: str                     # the schema.json path, relative to the repo root
    schema_module: str              # the module whose get_schema(name) compiles its .schema files
    capture_key_schema: str         # the schema's name in a capture's freshness key: the path it had when the keys were
                                    # first written, kept so a move of the file leaves every capture fresh
    scale_dir: str                  # a scale's dir name: "sf{scale}" (LDBC), "{scale}" (a data tag)
    output_root: str                # its dir name in every kind of the output tree (output_tree.place)
    records_source: str             # the module that names its record stream's data and dump (datasets/<name>/records.py)
    fetch_module: str               # the module that gets its data at a scale (datasets/<name>/fetch.py): plan(scale),
                                    # missing(scale), ensure(scale), check(scale); kvevaluator.fetch_dataset runs it
    mapping_module: object = None
    store_knobs: dict = field(default_factory=dict)   # {format: {knob: value}} every store of this dataset is built with
    workload_files: list = field(default_factory=list)   # the dirs of its workload files, relative to the repo root
                                    # ("{scale}": a per-scale dir, LinkBench's request log); kvcost's benchmarks.workloads
    measure_peak_gb: dict = field(default_factory=dict)   # {scale: the peak memory recorded for one store's measuring
                                    # call, in GB}; a scale with none is not capped by memory (run_measure.stores_at_once)

    def _mapped(self, name):
        if "{mapping}" in name:
            return name.format(mapping=importlib.import_module(self.mapping_module).MAPPING)
        return name

    def output_root_name(self):
        """the output tree's dir name: `ldbc`, `linkbench`, `imdb-<mapping>`"""
        return self._mapped(self.output_root)

    def scale_name(self, scale):
        """a scale's dir name: sf3 (LDBC), n1m (a data tag)"""
        return self.scale_dir.format(scale=scale)

    @property
    def own_axis(self):
        """its scale is a data tag, with its own store and output trees (LinkBench, IMDb), not LDBC's sf"""
        return self.scale_axis == "tag"

    def fetch(self):
        """its fetch_module: plan(scale), missing(scale), ensure(scale), check(scale) (kvevaluator.fetch_dataset)"""
        return importlib.import_module(self.fetch_module)

    def records(self):
        """its records_source module: source(), dump_argv(), check_dumpable(), DUMP_IMPORTS (measure/records.py)"""
        return importlib.import_module(self.records_source)


def fetch_hint(dataset, scale):
    """the command that gets a dataset's data at a scale, for a message that finds it missing: just fetch-dataset ldbc=3"""
    return f"just fetch-dataset {get(dataset).name}={scale}"


def schema(name, design="canonical"):
    """a dataset's compiled schema (its schema module's get_schema; LDBC's canonical one is LDBC_SCHEMA)"""
    return importlib.import_module(get(name).schema_module).get_schema(design)


def schema_file(name, design="canonical"):
    """where a dataset's compiled schema JSON lives, whether or not it is written yet: its manifest's `schema`, or for
    another design the `schema_<design>.json` beside it"""
    from kvevaluator.paths import ROOT
    base = os.path.join(str(ROOT), get(name).schema)
    return base if design == "canonical" else os.path.join(os.path.dirname(base), f"schema_{design}.json")


def data_in(dataset, scale):
    """a dataset's own files at a scale, in the data dir: <data dir>/datasets/<dataset>/<scale>/ (LDBC's CSVs as fetched;
    LinkBench's generated data/, anchors.txt and params/)"""
    from kvevaluator.paths import datasets_dir
    return datasets_dir() / dataset / get(dataset).scale_name(scale)


def params_in(dataset, scale, query_set):
    """a query set's tracked params at a scale: inputs/params/<dataset>/<scale>/<query set>/"""
    from kvevaluator.paths import INPUTS_DIR
    return INPUTS_DIR / "params" / dataset / get(dataset).scale_name(scale) / query_set


def latency_picks_in(dataset, scale, family):
    """the body each store times per query of a read family at a scale (kvcost measure-latency --save-picks writes it,
    the timing reads it): inputs/latency-picks/<dataset>/<scale>/<family>.json"""
    from kvevaluator.paths import INPUTS_DIR
    return INPUTS_DIR / "latency-picks" / dataset / get(dataset).scale_name(scale) / f"{family}.json"


def frequencies_in(dataset, scale=None):
    """the benchmark's own query frequencies at a scale: inputs/frequencies/<dataset>/<scale>/ (no scale: the dataset's
    dir, one subdir per scale)"""
    from kvevaluator.paths import INPUTS_DIR
    d = INPUTS_DIR / "frequencies" / dataset
    return d if scale is None else d / get(dataset).scale_name(scale)


def schema_path(name, design="canonical"):
    """the absolute path of a dataset's compiled schema JSON (schema_file); a missing file raises"""
    path = schema_file(name, design)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{name}: no compiled schema {path} (design {design!r}; "
                                f"python -m kvevaluator.datasets write-schema <name> writes it)")
    return path


def _load():
    rows = []
    for p in sorted(glob.glob(os.path.join(HERE, "*", "manifest.json"))):
        d = json.load(open(p))
        if d["name"] != os.path.basename(os.path.dirname(p)):
            raise SystemExit(f"{p}: name {d['name']!r} is not its folder's")
        rows.append(Dataset(**d))
    return {d.name: d for d in sorted(rows, key=lambda d: d.order)}


DATASETS = _load()


def names():
    return list(DATASETS)


def get(name):
    if name not in DATASETS:
        raise SystemExit(f"unknown dataset {name!r} (have: {', '.join(DATASETS)})")
    return DATASETS[name]
