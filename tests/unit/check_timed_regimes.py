#!/usr/bin/env python3
"""The cache regimes are one registry (inputs/contract/store_config.json: timed_regimes and its bounded variant), read
through kvevaluator.measure.store_config by the evaluator's timed runs and by the live databases alike: regimes() names
them, regime() gives one regime's whole settings, engines/env.sh sets the engines' caches from it, and
experiments/engine_fidelity/time_queries.py offers exactly its names. Store-free, no engine started.

    python3 tests/unit/check_timed_regimes.py
"""
import os
import subprocess
import sys

from kvevaluator.measure import store_config as SC
from kvevaluator.paths import ROOT

SF = "0.3"
FIELDS = ("block_cache_mib", "direct_io", "index_filter_in_cache", "cold_start")


def env_sh(regime, setting="equivalent"):
    """(rc, {name: value} of the caches and the regime's fields engines/env.sh sets, its stderr)"""
    names = ("DGRAPH_CACHE_MB NEBULA_CACHE_MB ARANGODB_CACHE_MB ENGINES_REGIME_BLOCK_CACHE_MIB ENGINES_REGIME_DIRECT_IO "
             "ENGINES_REGIME_INDEX_FILTER_IN_CACHE ENGINES_REGIME_COLD_START")
    r = subprocess.run(["bash", "-c", f"source engines/env.sh > /dev/null || exit 3; for k in {names}; do echo \"$k=${{!k-}}\"; done"],
                       cwd=ROOT, env=dict(os.environ, SF=SF, ENGINES_SETTING=setting, ENGINES_REGIME=regime),
                       capture_output=True, text=True)
    return r.returncode, dict(line.split("=", 1) for line in r.stdout.split()), r.stderr


def main():
    cases = []
    steady = SC.steady_cache_mib(SF)
    whole = SC.decided()["rocksdb"]["block_cache_bytes"] >> 20
    cases.append(("1 the names: the three timed regimes, then the bounded variant of warm and cold",
                  SC.regimes() == ["warm", "cold", "steady", "warm-bounded", "cold-bounded"]))
    got = {r: tuple(SC.regime(r, SF)[k] for k in FIELDS) for r in SC.regimes()}
    cases.append(("2 warm, steady and cold as the block decides them: the cache, direct reads, a cold start",
                  (got["warm"], got["steady"], got["cold"])
                  == ((whole, False, None, False), (steady, True, None, False), (whole, True, None, True))))
    cases.append(("3 the bounded variant: steady's cache, the OS page cache under it, index and filter blocks outside; "
                  "cold-bounded differs from warm-bounded in the cold start alone",
                  (got["warm-bounded"], got["cold-bounded"]) == ((steady, False, False, False), (steady, False, False, True))))
    try:
        SC.regime("small-cache", SF)
        refused = ""
    except ValueError as e:
        refused = str(e)
    cases.append(("4 a name the registry does not hold is refused, with its names", all(r in refused for r in SC.regimes())))
    ok = True
    for r in SC.regimes():
        rc, env, _ = env_sh(r)
        want = SC.regime(r, SF)
        flag = lambda v: "" if v is None else str(int(v))
        ok &= rc == 0 and {env[k] for k in ("DGRAPH_CACHE_MB", "NEBULA_CACHE_MB", "ARANGODB_CACHE_MB",
                                             "ENGINES_REGIME_BLOCK_CACHE_MIB")} == {str(want["block_cache_mib"])}
        ok &= (env.get("ENGINES_REGIME_DIRECT_IO"), env.get("ENGINES_REGIME_INDEX_FILTER_IN_CACHE"),
               env.get("ENGINES_REGIME_COLD_START")) == (flag(want["direct_io"]), flag(want["index_filter_in_cache"]),
                                                         flag(want["cold_start"]))
    cases.append((f"5 engines/env.sh at equivalent: the three block caches and the regime's fields are the registry's, "
                  f"for each of its {len(SC.regimes())} regimes", ok))
    rc_unknown, _, err_unknown = env_sh("small-cache")
    rc_counting, _, err_counting = env_sh("cold-bounded", setting="counting")
    rc_warm, env_warm, _ = env_sh("warm", setting="counting")
    cases.append(("6 engines/env.sh refuses a name outside the registry, and a regime other than warm at counting; "
                  "counting's own caches are untouched",
                  rc_unknown == 3 and "not a regime of the registry" in err_unknown and rc_counting == 3
                  and "needs ENGINES_SETTING=equivalent" in err_counting and rc_warm == 0
                  and env_warm["DGRAPH_CACHE_MB"] == env_warm["NEBULA_CACHE_MB"] == "4096"))
    tool = subprocess.run([sys.executable, "experiments/engine_fidelity/time_queries.py", "live", "--regime", "small-cache"],
                          cwd=ROOT, capture_output=True, text=True)
    cases.append(("7 time_queries.py --regime offers the registry's names and no other",
                  tool.returncode == 2 and "{" + ",".join(SC.regimes()) + "}" in tool.stderr.replace("\n", "").replace(" ", "")))
    bad = [name for name, good in cases if not good]
    for name, good in cases:
        print(f"{'ok  ' if good else 'FAIL'} {name}")
    print(f"check_timed_regimes {'OK' if not bad else 'FAILED'}: {len(cases) - len(bad)}/{len(cases)} cases")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
