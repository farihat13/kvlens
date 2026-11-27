"""paths.py finds the four locations as documented: defaults under the root, the environment over .env, a relative
value from the root, a bad .env line or key refused, a location inside another checkout refused; and the data dir's two
parts (datasets/, results/) and the dirs below them, with a data dir in the old layout refused, naming each move.

    python tests/lint/check_locations.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from kvevaluator import paths
from kvevaluator.paths import ROOT, PathsError, resolve


def expect_error(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except PathsError:
        return True
    return False


def with_env(env, fn):
    """fn() with these KVEVAL_* variables set in this process (the part functions read the environment when called)"""
    keep = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return fn()
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def refusal(env):
    """the PathsError text datasets_dir() raises under env, or None"""
    try:
        with_env(env, paths.datasets_dir)
    except PathsError as e:
        return str(e)
    return None


def main():
    results = []
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        none = d / "no.env"
        loc = resolve({}, none)
        results.append(("defaults", loc["data_dir"] == ROOT / "data" and loc["output_dir"] == ROOT / "output"
                        and loc["cpp_bin"] == ROOT / "build" / "cpp" / "kvevaluator"))
        env = d / "a.env"
        env.write_text(f"# a comment\nKVEVAL_DATA_DIR = {d / 'from-file'}\nKVEVAL_BUILD_DIR='{d / 'b'}'\n")
        loc = resolve({"KVEVAL_DATA_DIR": str(d / "from-env")}, env)
        results.append(("env wins over .env", loc["data_dir"] == d / "from-env"))
        results.append((".env read, quotes stripped", loc["build_dir"] == d / "b"))
        results.append(("binary follows the build dir", loc["cpp_bin"] == d / "b" / "cpp" / "kvevaluator"))
        results.append(("relative value from the root", resolve({"KVEVAL_OUTPUT_DIR": "out2"}, none)["output_dir"]
                        == ROOT / "out2"))
        for i, bad in enumerate(["KVEVAL_ROOT=/x", "KVEVAL_DATA_DIR=$HOME/d", "KVEVAL_DATA_DIR=~/d", "not a line"]):
            (d / f"bad{i}.env").write_text(bad + "\n")
            results.append((f".env refuses {bad!r}", expect_error(resolve, {}, d / f"bad{i}.env")))
        other = d / "other-checkout"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        results.append(("refused inside another checkout",
                        expect_error(resolve, {"KVEVAL_OUTPUT_DIR": str(other / "out")}, none)))
        results.append(("module constants match resolve()", paths.CPP_BIN == resolve()["cpp_bin"]))
        new = {"KVEVAL_DATA_DIR": str(d / "new")}
        got = with_env(new, lambda: (paths.datasets_dir(), paths.ldbc_dir("0.1"), paths.ldbc_params_dir("0.1"),
                                     paths.imdb_dir()))
        results.append(("the datasets part and the dataset dirs below it", got == (
            d / "new" / "datasets", d / "new" / "datasets" / "ldbc" / "sf0.1",
            d / "new" / "datasets" / "ldbc" / "substitution_parameters-sf0.1", d / "new" / "datasets" / "imdb")))
        results.append(("KVEVAL_IMDB_DIR wins over <data>/datasets/imdb",
                        with_env({**new, "KVEVAL_IMDB_DIR": str(d / "imdb2")}, paths.imdb_dir) == d / "imdb2"))
        (d / "new" / "datasets").mkdir(parents=True)
        results.append(("a data dir holding only datasets/ is accepted", refusal(new) is None))
        (d / "old" / "datasets").mkdir(parents=True)
        (d / "old" / "results").mkdir()
        why = refusal({"KVEVAL_DATA_DIR": str(d / "old")}) or ""
        results.append(("an entry beside datasets/ is refused, by name", str(d / "old") in why and "results" in why))
    for name, ok in results:
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    bad = sum(not ok for _, ok in results)
    print(f"check_locations {'FAIL' if bad else 'OK'}: {len(results) - bad}/{len(results)} cases")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
