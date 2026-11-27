"""A workload file: queries, how often each runs, and the dataset and scale they run at (kvcost's --workload).

    {"id": "my", "dataset": "ldbc", "scale": "0.1",
     "items": [{"family": "interactive", "query": "myq1", "frequency": 1, "params": null}, ...]}

`family` is the query set the query belongs to (kvevaluator.workloads); the dataset follows from the families when the
file does not name it. The file is only an input: every kvcost command writes into the output tree's kinds
(python/kvevaluator/output_tree.py), its places named by the dataset, the scale and the file's id, as the paper's
workloads are (python/kvcost/cli/README.md, "Where each command writes").
"""
import glob
import json
import os
from dataclasses import dataclass, field

from kvevaluator import datasets, workloads


@dataclass(frozen=True)
class Item:
    family: str
    query: str
    frequency: float
    params: object          # 1-based param indexes, or None: the workload's default params


@dataclass(frozen=True)
class WorkloadFile:
    """one workload file at one scale"""
    path: str
    name: str               # its id (else its name): the name of its places in the output tree
    dataset: str
    scale: str
    items: tuple
    raw: dict = field(compare=False, repr=False)

    def families(self):
        """the query sets the file's queries come from, in item order"""
        out = []
        for it in self.items:
            if it.family not in out:
                out.append(it.family)
        return out

    def queries(self, family=None):
        """the file's queries (of one family), in item order, each once"""
        out = []
        for it in self.items:
            if (family is None or it.family == family) and it.query not in out:
                out.append(it.query)
        return out


def _dataset_of(path, items, named):
    ds = {workloads.get(it.family).dataset for it in items if it.family in workloads.WORKLOADS}
    unknown = sorted({it.family for it in items if it.family not in workloads.WORKLOADS})
    if unknown:
        raise SystemExit(f"{path}: family {unknown[0]!r} is not a workload (have: {', '.join(workloads.names())})")
    if named:
        datasets.get(named)
        if ds - {named}:
            raise SystemExit(f"{path}: dataset {named!r}, but its items' families run on {sorted(ds)}")
        return named
    if len(ds) != 1:
        raise SystemExit(f"{path}: its items run on {len(ds)} datasets ({sorted(ds)}): one workload file is one dataset")
    return ds.pop()


def load(path, scale=None):
    """the WorkloadFile of a workload file; `scale` (the command's --dataset scale) wins over the file's own"""
    if not os.path.exists(path):
        raise SystemExit(f"--workload {path}: no such file")
    with open(path) as f:
        raw = json.load(f)
    items = tuple(Item(it["family"], it["query"], it["frequency"], it.get("params")) for it in raw.get("items", []))
    if not items:
        raise SystemExit(f"{path}: no items (a workload file lists its queries under `items`)")
    ds = _dataset_of(path, items, raw.get("dataset"))
    sc = scale if scale is not None else raw.get("scale")
    if sc is None:
        raise SystemExit(f"{path}: no scale: give --dataset {ds}=<scale> or a `scale` in the file")
    return WorkloadFile(path=os.path.abspath(path), name=raw.get("id") or raw.get("name"), dataset=ds, scale=str(sc),
                        items=items, raw=raw)


def dirs(dataset=None, scale=None):
    """the dirs a dataset's workload files are in at a scale (its manifest's workload_files; every scale's when none is
    given), or every dataset's when none is given"""
    from kvevaluator.paths import ROOT
    names = [dataset] if dataset else sorted(datasets.DATASETS)
    return [os.path.join(str(ROOT), d.format(scale=scale if scale is not None else "*"))
            for n in names for d in datasets.get(n).workload_files]


def files(dataset=None, scale=None):
    """[(path, its JSON)]: every workload file (a JSON with `items`) in dirs(dataset, scale) and below, sorted by path"""
    out = []
    for p in sorted({p for d in dirs(dataset, scale) for p in glob.glob(os.path.join(d, "**", "*.json"), recursive=True)}):
        try:
            with open(p) as f:
                w = json.load(f)
        except ValueError:
            continue
        if isinstance(w, dict) and "items" in w:
            out.append((p, w))
    return out


def resolve(text, dataset=None, scale=None):
    """The one way a workload argument (--workload, a question's workload, a driver's) names a file: `text` itself when it
    is a file, else the one workload file whose id or name is `text`, among the dataset's at the scale (all datasets' or
    scales' when not given). Refused when none or several match: an id names one file per dataset and scale."""
    if os.path.isfile(text):
        return text
    hits = [p for p, w in files(dataset, scale) if text in (w.get("id"), w.get("name"))]
    if len(hits) == 1:
        return hits[0]
    where = ", ".join(os.path.relpath(d) for d in dirs(dataset, scale))
    if not hits:
        raise SystemExit(f"--workload {text}: not a file, and no workload file under {where} has that id or name")
    fix = "give the file" if dataset and scale is not None else "give --dataset with its scale, or the file"
    raise SystemExit(f"--workload {text}: {len(hits)} workload files have that id or name ({', '.join(hits)}): {fix}")


def datasets_holding(text, run):
    """the datasets of a run ({dataset: [scale, ...]}) whose workload files at the run's first scale hold the workload
    `text` (an id or a name)"""
    return [ds for ds, scales in run.items()
            if any(text in (w.get("id"), w.get("name")) for _p, w in files(ds, scales[0]))]
