"""`just fetch-dataset`: get every dataset a run needs, at its scales, each step only if missing; or check what is in
place against the tracked hashes and download nothing.

    just fetch-dataset                                  the paper's scales (kvevaluator.sizes.PAPER: ldbc=3,linkbench=n1m,imdb=full)
    just fetch-dataset ldbc=0.1,imdb=sample1pct         those datasets at those scales
    just fetch-dataset ldbc=0.1,imdb=sample1pct --check verify what is in place; download and make nothing

Per dataset, its manifest's fetch_module does the work (plan, missing, ensure, check; datasets/<name>/fetch.py): LDBC
downloads LDBC's archives, IMDb downloads the dump and the query texts and builds the tables, LinkBench builds the tool
and generates the data and the params. Then each workload on the dataset with a data step (its manifest's data_step:
JOB's reference answers) runs it. Before any work, one plan line per dataset: what is present, what is to fetch, the
download and disk size, and the recorded time (from inputs/data-sources/<dataset>/). A message that finds data missing
names this command (kvevaluator.datasets.fetch_hint).
"""
import argparse
import importlib
import sys
import time

from kvevaluator import datasets, sizes, workloads


def data_steps(dataset):
    """the data-step modules of the workloads on a dataset (their manifests' data_step), in workload order"""
    names = [workloads.get(w).data_step for w in workloads.names(dataset=dataset)]
    return [importlib.import_module(n) for n in dict.fromkeys(filter(None, names))]


def missing(dataset, scale):
    """[what is not in place] for the dataset at the scale: its data, then its workloads' data steps (existence only)"""
    return datasets.get(dataset).fetch().missing(scale) + [m for s in data_steps(dataset) for m in s.missing(scale)]


def ensure(dataset, scale):
    """the dataset's data at the scale, then its workloads' data steps, each step only if missing; [problem]"""
    bad = datasets.get(dataset).fetch().ensure(scale)
    for s in ([] if bad else data_steps(dataset)):
        bad += s.ensure(scale)
    return bad


def check(dataset, scale):
    """the dataset's files at the scale against their hashes (and its workloads' data steps); ([problem], files checked)"""
    bad, n = datasets.get(dataset).fetch().check(scale)
    for s in data_steps(dataset):
        b, k = s.check(scale)
        bad, n = bad + b, n + k
    return bad, n


def missing_message(dataset, scale):
    """None when the dataset's data at the scale is in place, else one line: what is missing and the command that makes it"""
    gone = missing(dataset, scale)
    if not gone:
        return None
    return (f"no {dataset} data at scale {scale} ({len(gone)} missing, first: {gone[0]}): `{datasets.fetch_hint(dataset, scale)}` "
            f"gets it, each step only if missing")


def gb(x):
    return f"{x / 1e9:.2f} GB" if x >= 1e8 else f"{x / 1e6:.0f} MB"


def plan_line(dataset, scale):
    """one line: what is present, what is to fetch, the download and disk size, the recorded time"""
    p = datasets.get(dataset).fetch().plan(scale)
    extra = [m for s in data_steps(dataset) for m in s.missing(scale)]
    head = f"[fetch-dataset] {dataset}={scale}: present: {', '.join(p['present']) or 'nothing'}"
    if not p["to_fetch"] and not extra:
        return head + "; nothing to fetch"
    if not p["to_fetch"]:
        return f"{head}; to make: {len(extra)} files of its workloads' data steps (seconds; no download)"
    todo = p["to_fetch"] + ([f"{len(extra)} files of its workloads' data steps"] if extra else [])
    took = (f"about {p['seconds'] / 60:.0f} min" if p["seconds"] >= 90 else f"about {p['seconds']:.0f} s") \
        if p.get("seconds") is not None else "time not recorded"
    down = "download not recorded" if p["download_bytes"] is None else f"download {gb(p['download_bytes'])}"
    return (f"{head}; to fetch: {', '.join(todo)}: {down}, disk {gb(p['disk_bytes'])}, "
            f"{took}{' (recorded ' + p['date'] + ')' if p.get('date') else ''}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("datasets", nargs="?", default=sizes.PAPER,
                    help=f"each dataset's scale, e.g. ldbc=0.1,imdb=sample1pct (default the paper's: {sizes.PAPER})")
    ap.add_argument("--check", action="store_true", help="verify what is in place against the tracked hashes; "
                    "download and make nothing")
    a = ap.parse_args(argv)
    run = sizes.resolve(a.datasets)
    pairs = [(ds, sc) for ds, scs in run.items() for sc in scs]
    for ds, sc in pairs:
        print(plan_line(ds, sc), flush=True)
    done, bad = [], []
    for ds, sc in pairs:
        t0 = time.monotonic()
        if a.check:
            b, n = check(ds, sc)
            if n == 0:
                b = b + [f"{ds}={sc}: no file checked"]
            done.append(f"{ds}={sc} {n} files")
        else:
            b = ensure(ds, sc)
            done.append(f"{ds}={sc} {time.monotonic() - t0:.0f} s")
        bad += b
    verb = "check" if a.check else "fetch"
    print(f"fetch-dataset {'OK' if not bad else 'FAIL'} ({verb}): {'; '.join(done)}"
          + (f"; {len(bad)} problems" if bad else ""), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
