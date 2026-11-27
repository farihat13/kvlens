"""kvcost: the KV cost of a workload's queries on each layout, and the best mix.

One workload, start to end (a workload file lists its queries; every step names the dataset and its scale, --dataset
ldbc=0.1, or the file a "scale"; every step writes only into the output dir's kinds (KVEVAL_OUTPUT_DIR): captures/,
composed/, stores/, timings/, summary/<dataset>/<scale>/; --from TREE reads another tree, e.g. paper-results;
python/kvcost/cli/README.md, "Where each command writes"):
  kvcost generate-plans  --workload my.json --dataset ldbc=0.1     each query's plan file, printed
  kvcost measure         --workload my.json --dataset ldbc=0.1 Sp-Sp Co-Co Ch64k-Ch64k
                                                                    the workload's query sets counted on each layout
  kvcost compose         --dataset ldbc=0.1 [Sp-Ch4k ...]           the layouts not measured, composed from the
                                                                    measured diagonals (default every pair of them)
  kvcost cost-layouts    --workload my.json --dataset ldbc=0.1 [Sp-Co Co-Ch4k ...]
                                                                    each query's plan and cost on each layout, every
                                                                    model (layout-costs/<workload>/measured/)
  kvcost cost-layouts    --workload my.json --dataset ldbc=0.1 --mix "knows lists=Ch64k" --mix-base Co-Sp
                         --mix-name knows-chunked                   a hand-written mix priced from the parts, no store
  kvcost list-parts      --workload my.json --dataset ldbc=0.1      the parts a mix string can name
  kvcost plot-costs      --workload my.json --dataset ldbc=0.1      per query over Sp-Co, and the totals
  kvcost search-mix      --workload my.json --dataset ldbc=0.1 --cost-model wops_bytes4k
                                                                    the best mix (--front: the wops-bytes front)
  kvcost check-mix       --workload my.json --dataset ldbc=0.1 mix:best build the found mix, measure it: measured = composed
  kvcost measure-latency --workload my.json --dataset ldbc=0.1 Co-Co mix:best
                                                                    the mix and a layout timed on their stores
  kvcost record-picks    --workload my.json                         the plan picks of this workload, recorded

Layouts are named, any chunk size: Sp-Co, Co-Ch4k, Ch1k-Ch16k (the grid's 64 KB is Ch64k), a cell id or a mix's spec file.
The paper's forms (run/reproduce.py runs them, the layouts written out): measure ... Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k
Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k, cost-layouts ... <the 49: Sp-Sp Sp-Co ... Ch256k-Ch256k>, search-mix --question
NAME, predict ... <the 49> --tier 1|2, predict-accuracy ... <the 49>, measure-latency ... <the 12> <questions>.

Predict mode (statistics, no store): kvcost build-statistics makes the statistics and tallies; kvcost predict writes
predicted captures; kvcost cost-layouts --count-source <set> prices them; kvcost predict-accuracy --count-source <set>
--against measured compares the two (any two count sources: A's pick on B, the spread, Spearman;
python/kvcost/cli/predict.py).

python/kvcost/cli/README.md: what each command calls and writes, and the models.
"""
import argparse
import sys

from kvcost.cli import workloads as W
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import picks
from kvevaluator import sizes


def plan_rule(ad, a):
    """the plan rule of this run: --pick-on, else the dataset's default (LinkBench, the adapter's IN_SAMPLE: one body
    per request and no held-out set, so in-sample; otherwise held-out)"""
    held_out_set = not getattr(ad, "IN_SAMPLE", False)
    return picks.pick_on_rule(benchmarks.pick_on_of(a, held_out_set), held_out_set)


class DatasetScale(argparse.Action):
    """--dataset ldbc=0.1, or a run (ldbc=3,imdb=full, or the flag repeated): the whole run in a.run ({dataset: [main
    scale, further scales]}: kvevaluator.sizes.resolve, every flag merged in order), and the main pair in a.dataset and
    a.sf: the first dataset of the run in the datasets' order (LDBC's when it holds LDBC) at its first scale. The only
    way to name a scale. A command that runs one dataset refuses a run of several pairs (runs.one_pair)"""

    def __call__(self, parser, namespace, value, _option_string=None):
        if "=" not in value:
            parser.error(f"--dataset {value}: give dataset=scale, e.g. ldbc=0.1 (datasets: {', '.join(benchmarks.DATASETS)})")
        run = {ds: list(scs) for ds, scs in (getattr(namespace, "run", None) or {}).items()}
        for ds, scs in sizes.resolve(value).items():
            for sc in scs:
                if sc in run.get(ds, []):
                    parser.error(f"--dataset: {ds}={sc} named twice")
                run.setdefault(ds, []).append(sc)
        namespace.run = sizes.resolve(sizes.text(run))          # the datasets' order (the registry's), each's scales in order
        namespace.dataset, namespace.sf = next((ds, scs[0]) for ds, scs in namespace.run.items())


def parser():
    """the kvcost command line: every command and its flags"""
    ap = argparse.ArgumentParser(prog="kvcost", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # one list of the commands (below, each with its help); a metavar naming them all would print them twice
    sub = ap.add_subparsers(dest="sub", required=True, metavar="<command>")

    def common(p, run=False, picks=True):
        """the flags every command shares; run=True: a workload's command, whose benchmark and scale may come from its
        workload file; picks=False: a command whose output no plan pick and no param set changes (list-parts,
        generate-plans) takes neither --pick-by nor --params"""
        p.add_argument("--dataset", required=not run, default=None, action=DatasetScale, metavar="DATASET=SCALE",
                       help="the dataset and its scale, e.g. ldbc=0.1, imdb=sample1pct, linkbench=n1m (kvevaluator."
                            "sizes; its adapter: the dataset's, or BI's for a BI workload). A command that spans "
                            "datasets (search-mix --question) takes a run: ldbc=3,imdb=full, or the flag repeated"
                            + ("; default: the workload's" if run else ""))
        p.set_defaults(sf=None)        # the scale, from --dataset only
        if not picks:
            p.set_defaults(criterion=None, param_set=None)
            p.set_defaults(out=None)
            return
        benchmarks.add_pick_by_arg(p)
        benchmarks.add_params_arg(p, help="which param set, part of the capture's identity. LinkBench: log (the "
                                           "default: the whole request log, 100,000 requests, as published), quick (each "
                                           "request type's first requests, 5 with the default --nparam: the one to use "
                                           "on tiny), quick-next (the next ones: a second sample), anchors (degree-"
                                           "stratified nodes, reads only); LDBC Interactive: ldbc (the default) or "
                                           "uniform")
        p.set_defaults(out=None)       # every command writes into the output dir (KVEVAL_OUTPUT_DIR), at its place

    def with_adapter(fn):              # the pricing verbs: the benchmark's adapter and its plan rule
        def run(a, argv):
            ad = W.get(a.adapter)
            ad.prepare(a)
            return fn(a, argv, ad, plan_rule(ad, a))
        return run
    from kvcost.cli import search as SR
    SR.add_parsers(sub, lambda p, **k: common(p, run=True, **k), with_adapter)
    from kvcost.cli import verbs as VB
    VB.add_parsers(sub, lambda p, **k: common(p, run=True, **k))
    from kvcost.cli import latency as LT
    LT.add_parsers(sub, lambda p: common(p, run=True))
    from kvcost.cli import predict as PR
    PR.add_parsers(sub)
    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    a = parser().parse_args(argv)
    return a.fn(a, argv)


if __name__ == "__main__":
    sys.exit(main())
