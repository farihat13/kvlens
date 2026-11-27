"""kvcost's workload adapters, one module each: how a dataset's loads are priced and its data precondition.

An adapter module defines: NAME, DATASET (a kvevaluator.datasets name), FAMILIES (the (family, kind) pairs its loads
read: benchmarks.families(DATASET), or BI alone), SCALE_HELP, WEIGHTED, data_missing(sf) -> None or the command that
makes the data, prepare() (any per-process setup) and view_name(); optionally NPARAM, TIMEOUT and IN_SAMPLE. No adapter
imports another; shared logic lives in python/kvcost/cli/.
To add one: a module here, its line in ADAPTERS, and what any grid benchmark needs (README "Adding a benchmark").
"""
import importlib

ADAPTERS = ("ldbc", "bi", "linkbench", "imdb")
NPARAM = 5          # the reported params per query a measure or a prediction takes when the adapter names no count


def nparam_of(name, param_set=None):
    """the params per query of a load priced by adapter `name` on `param_set`: the adapter's NPARAM entry for the set
    (LinkBench's whole log: 100,000; JOB's one param), else NPARAM. The one default of measure, check-mix and predict"""
    return getattr(get(name), "NPARAM", {}).get(param_set, NPARAM)


def place_pick_on(dataset, families, pick_on):
    """--pick-on as a layout-costs place names it (layout_costs.place_of_run): None for the dataset's default
    (benchmarks.pick_on_of: held-out, or in-sample where the adapter is IN_SAMPLE: LinkBench), as when none is given;
    else the value"""
    from kvcost.layout_cost import benchmarks
    held_out_set = not getattr(get(of(dataset, families)), "IN_SAMPLE", False)
    return None if pick_on == benchmarks.pick_on_of(None, held_out_set) else pick_on


def timeout_of(name):
    """seconds per run before it is recorded as not ok, for adapter `name`: its TIMEOUT (JOB: 3600), else None
    (grid_bodies' own default, 1800)"""
    return getattr(get(name), "TIMEOUT", None)


def of(dataset, families=()):
    """the adapter that prices a load of these families on the dataset: its own, or, for a load of an optional family
    (LDBC's BI: priced apart from the dataset's other families), that family's; a mix of the two is refused"""
    from kvcost.layout_cost import benchmarks
    fams = set(families or ())
    opt = fams & set(benchmarks.OPTIONAL.get(dataset, {}))
    if not opt:
        return dataset
    if fams != opt or len(opt) != 1:
        raise SystemExit(f"{dataset}: {', '.join(sorted(opt))} is priced apart from the dataset's other families "
                         f"(one load: {' or '.join(sorted(opt))} alone); this one reads {', '.join(sorted(fams))}")
    return opt.pop()


def get(name):
    if name not in ADAPTERS:
        raise SystemExit(f"unknown workload {name!r} (have: {', '.join(ADAPTERS)})")
    try:
        return importlib.import_module(f"kvcost.cli.workloads.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"kvcost.workloads.{name}":
            raise SystemExit(f"the {name} adapter is not built yet")
        raise
