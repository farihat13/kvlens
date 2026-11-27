"""The decided store and timing settings (inputs/contract/store_config.json: the decided block of the study's store-config doc), and the
one rule that compares a timed run's record with them: check_store_config.py (the round's check) and
capture_protocol.resume_mismatch (a driver about to reuse a capture) both call `violations`."""
import glob
import json
import math
import os

from kvevaluator.paths import INPUTS_DIR

DECIDED = INPUTS_DIR / "contract" / "store_config.json"     # the decided block of the study's store-config doc, as a tracked input


def decided(path=DECIDED):
    """the decided store and timing settings as a dict (inputs/contract/store_config.json)"""
    with open(path) as f:
        return json.load(f)


def steady_cache_mib(sf, rule=None, dataset=None):
    """the steady block cache the block decides for a dataset: the fraction of the dataset's size. A numeric scale (an
    SF, nominal GiB): floor(fraction x SF x 1024) MiB (0.1: 307 at sf3, 10 at sf0.1, 3,072 at sf30); a scale tag (IMDb's
    full): floor(fraction x its CSV bytes / 2^20) MiB, the bytes from the block's dataset_bytes["<dataset>/<tag>"] (370 at
    imdb/full). ValueError for a tag the block has no size for"""
    rule = rule or decided()
    steady = rule["timed_regimes"]["steady"]
    try:
        scale = float(sf)
    except (TypeError, ValueError):
        scale = None
    if scale is not None:
        return math.floor(steady["block_cache_dataset_fraction"] * scale * 1024)
    size = (steady.get("dataset_bytes") or {}).get(f"{dataset}/{sf}")
    if size is None:
        raise ValueError(f"no steady cache rule for {dataset} at {sf}: inputs/contract/store_config.json's dataset_bytes has no {dataset}/{sf}")
    return math.floor(steady["block_cache_dataset_fraction"] * size / (1 << 20))


def regime_of(name):
    """a cpplatency regime key or dir name -> the block's regime: warm, cold, steady (else None)"""
    head = name.split("-")[0]
    return head if head in decided()["timed_regimes"] else None


def regimes(rule=None):
    """every regime's name: the block's timed regimes, and <regime>-bounded for each regime its `bounded` variant is of"""
    rule = rule or decided()
    return [*rule["timed_regimes"], *(f"{r}-bounded" for r in rule["bounded"]["of"])]


def regime(name, sf=None, dataset=None, rule=None):
    """one regime's whole settings, for every tool that times at it (the evaluator's stores and the live databases):
      block_cache_mib        the block cache: the steady size for the dataset at sf (steady, and the bounded
                             variant), else the store's own default (rocksdb.block_cache_bytes: everything fits)
      direct_io              a block-cache miss reads the disk, not the OS page cache
      index_filter_in_cache  index and filter blocks in the block cache (None: the regime does not say; the store's own)
      cold_start             the store is opened anew, nothing of it in memory, before every timed run; such a regime
                             has no warm-up
    ValueError for a name the block does not hold"""
    rule = rule or decided()
    if name not in regimes(rule):
        raise ValueError(f"no regime {name!r}: inputs/contract/store_config.json holds {', '.join(regimes(rule))}")
    base = regime_of(name)
    variant = rule["bounded"] if name != base else {}
    bounded = base == "steady" or variant.get("block_cache") == "steady"
    return {"name": name,
            "block_cache_mib": steady_cache_mib(sf, rule, dataset) if bounded else rule["rocksdb"]["block_cache_bytes"] >> 20,
            "direct_io": variant.get("direct_io", rule["timed_regimes"][base].get("direct_io", False)),
            "index_filter_in_cache": variant.get("cache_index_and_filter_blocks"),
            "cold_start": rule["timed_regimes"][base].get("cold_start", False)}


def violations(meta, rule=None):
    """(the fields compared, [what differs]) for one cpplatency run meta against the decided block. A regime that ran
    at a breakdown perf level in a capture that says it is a breakdown is held to the breakdown row instead (its perf
    level and the store options; a breakdown is never a wall of record, so its read path is free). Direct I/O is
    compared twice: the driver's flag and the server's own report (store_options.direct_io). A regime with no store
    options recorded (an older capture) is a violation: its settings are unknown. An option the record
    does not hold is not compared (store_option_diffs). The steady
    cache is compared twice too (the driver's block_cache_bytes and the server's), against steady_cache_mib(meta's sf)."""
    rule = rule or decided()
    compared, bad = 0, []
    # a breakdown is DECLARED: cpplatency --breakdown-pass, or the steady breakdown capture (it names its wall capture);
    # capture_kind alone is not, since cpplatency derives it from the perf level
    breakdown = bool(meta.get("breakdown_pass") or meta.get("wall_capture"))
    for name, spec in sorted((meta.get("regimes") or {}).items()):
        r = regime_of(name)
        want = rule["timed_regimes"].get(r) if r else None
        if want is None or not isinstance(spec, dict):
            continue
        level = spec.get("perf_level")
        held = rule["breakdown"] if breakdown and level in rule["breakdown"]["perf_level"] else want
        compared += 1
        if level not in held["perf_level"]:
            bad.append(f"{name}: perf_level {level}, want one of {held['perf_level']}")
        if "direct_io" in held:
            compared += 1
            if spec.get("direct_io") != held["direct_io"]:
                bad.append(f"{name}: direct_io {spec.get('direct_io')}, want {held['direct_io']}")
        so = spec.get("store_options")
        if not isinstance(so, dict):
            compared += 1
            bad.append(f"{name}: no store options recorded (an older capture or another driver's)")
            continue
        if "direct_io" in held:
            compared += 1
            if so.get("direct_io") != held["direct_io"]:
                bad.append(f"{name}: the server's direct_io {so.get('direct_io')}, want {held['direct_io']}")
        if r == "steady":                           # a breakdown pass too: another cache would be another regime
            sf = meta.get("sf")
            try:                                    # a LinkBench scale tag (n1m) has no steady rule: a difference
                from kvevaluator import workloads
                dataset = workloads.get(meta["workload"]).dataset if meta.get("workload") else None
                want_b = steady_cache_mib(sf, rule, dataset) << 20
            except (TypeError, ValueError):
                want_b = None
            for who, got in (("the driver's", spec.get("block_cache_bytes")), ("the server's", so.get("block_cache_bytes"))):
                compared += 1
                if want_b is None or got != want_b:
                    bad.append(f"{name}: {who} block_cache_bytes {got}, want {want_b} (the steady rule, store_config.steady_cache_mib, at sf {sf}"
                               f"{'' if want_b else ': no rule for this sf'})")
        n, b = store_option_diffs(name, so, rule)
        compared += n
        bad += b
    return compared, bad


def store_option_diffs(name, so, rule):
    """(the fields compared, [what differs]) for one pass's store options against the block's store_options and its
    rocksdb block. A key the record does not hold (a run from before the store reported it) is not compared: what is
    recorded is held to the file, what is not is unknown, never a refusal; a record that holds none of them is one. A
    rocksdb setting is read from the record's rocksdb block, else from its top level (compression and index_filter_blocks
    are there in every record); the block size is rocksdb.data_block_bytes, recorded as block_size"""
    bad, compared = [], 0
    want = dict(rule["store_options"], block_size=rule["rocksdb"]["data_block_bytes"])
    want.update({f"rocksdb.{k}": v for k, v in rule["rocksdb"].items() if k not in rule["run_flags"]})   # a flag: per run
    block = so.get("rocksdb") if isinstance(so.get("rocksdb"), dict) else {}
    for k, v in sorted(want.items()):
        got = block.get(k[len("rocksdb."):], so.get(k[len("rocksdb."):])) if k.startswith("rocksdb.") else so.get(k)
        if got is None:
            continue
        compared += 1
        if got != v:
            bad.append(f"{name}: store option {k} = {got}, want {v}")
    if not compared:
        return 1, [f"{name}: none of the decided store options is recorded"]
    return compared, bad


def steady_write_diffs(meta, rule):
    """(the fields compared, [what differs]) of one cppwlatency run meta against the steady read rule: direct I/O, the
    SF's steady cache and index + filter blocks in it, from the driver (read_settings) and from every pass's server"""
    held, sf = rule["timed_regimes"]["steady"], meta.get("sf")
    try:
        from kvevaluator import workloads
        dataset = workloads.get(meta["workload"]).dataset if meta.get("workload") else None
        want_b = steady_cache_mib(sf, rule, dataset) << 20
    except (TypeError, ValueError):
        want_b = None
    want = (("direct_io", held["direct_io"]), ("block_cache_bytes", want_b), ("cache_index_and_filter_blocks", True))
    sources = [("the driver's", meta.get("read_settings") or {})]
    sources += [(f"pass {name}: the server's", so or {}) for name, so in sorted(meta["store_options_by_pass"].items())]
    bad = [f"steady write: {who} {k} {got.get(k)}, want {v} (the steady rule, inputs/contract/store_config.json"
           f"{'' if v is not None else f': no rule at sf {sf}'})"
           for who, got in sources for k, v in want if v is None or got.get(k) != v]
    return len(sources) * len(want), bad


def write_violations(meta, rule=None, want=None):
    """(the fields compared, [what differs]) for one cppwlatency run meta: each pass's store options (as its server
    reported them) against the decided block; a meta with none recorded (an older capture) is a violation. The passes' perf levels are the driver's own (headline 1, breakdown 3: inputs/contract/store_config.json's perf levels).
    want = "steady" (default: the cache the meta's read_settings name; a meta from before them is warm): the steady read
    rule too (steady_write_diffs), as violations holds a steady read run to it."""
    rule = rule or decided()
    by_pass = meta.get("store_options_by_pass")
    if not isinstance(by_pass, dict) or not by_pass:
        return 1, ["no store options recorded (an older write capture)"]
    want = want or (meta.get("read_settings") or {}).get("cache") or "warm"
    compared, bad = steady_write_diffs(meta, rule) if want == "steady" else (0, [])
    for name, so in sorted(by_pass.items()):
        n, b = store_option_diffs(name, so or {}, rule)
        compared += n
        bad += b
    return compared, bad


READAHEAD_NOTE = ("RocksDB's iterator readahead was on, so block_read_count leaves out the blocks the readahead buffer "
                  "served (block_fetcher.cc: no count on a prefetch-buffer hit; inputs/contract/store_config.json): it is not "
                  "the block-cache misses. Read the cache's miss tickers instead (passes.csv block_cache_miss / data_miss)")


def block_reads_are_misses(capture):
    """True when a capture's block_read_count counts every block-cache miss: no run meta of it records the iterator
    readahead on. A capture with no store options recorded was made with
    max_auto_readahead_size = 0, so it reads as readahead off. `capture` is a dir holding steps.csv, or the steps.csv."""
    csv_path = capture if capture.endswith(".csv") else os.path.join(capture, "steps.csv")
    for p in sorted(glob.glob(csv_path + ".meta*.json")):
        with open(p) as f:
            m = json.load(f)
        found = [s.get("store_options") for s in (m.get("regimes") or {}).values() if isinstance(s, dict)]
        found += [m.get("store_options")] + list((m.get("store_options_by_pass") or {}).values())
        if any(isinstance(so, dict) and (so.get("max_auto_readahead_size") or 0) > 0 for so in found):
            return False
    return True


def require_block_reads_are_misses(capture, what):
    """refuse (exit) a reader that takes block_read_count as the block-cache misses on a capture timed with readahead on"""
    if not block_reads_are_misses(capture):
        raise SystemExit(f"{what}: {capture}: {READAHEAD_NOTE}")


if __name__ == "__main__":      # engines/env.sh: one regime's settings as key=value lines (True 1, False 0, None empty)
    import sys
    try:
        _name, _sf = sys.argv[1:3]
        for _k, _v in regime(_name, _sf).items():
            print(f"{_k}={'' if _v is None else int(_v) if isinstance(_v, bool) else _v}")
    except ValueError as _e:
        sys.exit(f"store_config: {' '.join(sys.argv[1:])}: {_e}" if len(sys.argv) == 3 else "usage: store_config.py <regime> <sf>")
