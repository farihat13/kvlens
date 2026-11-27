"""A run's scales, per dataset: one `--scale` value read the same way by the registry, every driver that spans
datasets and every derive. The value names each dataset's scale, and nothing else:

    ldbc=0.1,imdb=sample1pct                  LDBC at 0.1, IMDb (JOB) at sample1pct; LinkBench left out
    ldbc=3,linkbench=n1m,imdb=full            the paper's (PAPER, every default)
    ldbc=3,ldbc=30,linkbench=n1m,imdb=full    the paper's, and LDBC at 30 beside 3 (rows the paper does not report)

A dataset named twice has two scales: the first is its main one, the next ones feed the rows that set scales side by
side (SF30's rows beside SF3's: `--dataset ldbc=3,ldbc=30,...`). Each scale must be one its dataset's manifest lists (datasets.get(name).scales).
A dataset the value leaves out is not run; the code that would read it prints a line naming it. A tool that runs one
dataset (kvcost's commands) takes that dataset's plain scale instead.

    python -m kvevaluator.sizes scale <value> <dataset>     the dataset's main scale (empty: not in this run)
    python -m kvevaluator.sizes larger <value> <dataset>    its further scales, one a line
    python -m kvevaluator.sizes text <value>                the value in the datasets' order
    python -m kvevaluator.sizes paper                       the paper's value (PAPER)
"""
import argparse

from kvevaluator import datasets

PAPER = "ldbc=3,linkbench=n1m,imdb=full"   # the paper's runs: the default of every --dataset


def resolve(value=PAPER):
    """the run a --scale value names: {dataset: [main scale, further scales...]}, in the datasets' order"""
    value = str(value or PAPER)
    run = {}
    for part in filter(None, value.split(",")):
        ds, eq, sc = part.partition("=")
        if not eq or not sc:
            raise SystemExit(f"--dataset {value}: {part!r} names no dataset; write each as dataset=scale "
                             f"(e.g. ldbc=0.1,imdb=sample1pct; datasets: {', '.join(datasets.names())})")
        run.setdefault(datasets.get(ds).name, []).append(sc)
    for ds, scs in run.items():
        have = datasets.get(ds).scales
        for sc in scs:
            if sc not in have:
                raise SystemExit(f"--dataset {value}: {ds} has no scale {sc!r} (its manifest's scales: {', '.join(have)})")
        if len(set(scs)) != len(scs):
            raise SystemExit(f"--dataset {value}: {ds} names a scale twice")
    return {ds: run[ds] for ds in datasets.names() if ds in run}


def scale(run, dataset):
    """the dataset's main scale in `run` (resolve's dict), or None: not in this run"""
    return run.get(dataset, [None])[0]


def larger(run, dataset):
    """the dataset's further scales in `run`: what its side-by-side rows read beside the main one"""
    return run.get(dataset, [])[1:]


def text(run):
    """the run as a --scale value"""
    return ",".join(f"{ds}={sc}" for ds, scs in run.items() for sc in scs)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", choices=("scale", "larger", "text", "paper"))
    ap.add_argument("value", nargs="?", default=PAPER)
    ap.add_argument("dataset", nargs="?")
    a = ap.parse_args()
    if a.what == "paper":
        print(PAPER)
        return
    run = resolve(a.value)
    if a.what == "text":
        print(text(run))
    elif a.dataset is None:
        raise SystemExit(f"{a.what} needs a dataset")
    elif a.what == "scale":
        print(scale(run, datasets.get(a.dataset).name) or "")
    else:
        print("\n".join(larger(run, a.dataset)))


if __name__ == "__main__":
    main()
