"""Each engine's `ldbc-download` links the LDBC dirs with `ln -sfn`, so a second run replaces its link and does not put
a new link inside the data dir (which LDBC's fetch then refuses). Runs each recipe's ln lines twice on a temp dir; the
planted case, the same lines without -n, must leave links inside the data dir.

    python tests/unit/check_engine_ldbc_links.py
"""
import os
import re
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT

DBS = ("dgraph", "nebulagraph", "arangodb")
SF = "0.1"
LN = re.compile(r"^\s*(ln .*\$\{LDBC_SNB_DATA_DIR\}.*)$")


def recipe_ln_lines(db):
    """the ln lines of engines/<db>/justfile's ldbc-download recipe that link from LDBC_SNB_DATA_DIR"""
    text = (ROOT / "engines" / db / "justfile").read_text()
    recipe = re.search(r"^ldbc-download\b.*?(?=^\S|\Z)", text, re.S | re.M).group(0)
    return [m.group(1).replace("{{sf}}", SF) for line in recipe.splitlines() if (m := LN.match(line))]


def links_after_two_runs(lines):
    """run `lines` twice from an engine dir against a temp LDBC data dir -> links found inside the data dir"""
    with tempfile.TemporaryDirectory() as d:
        data, engine = os.path.join(d, "ldbc"), os.path.join(d, "engine")
        for sub in (f"sf{SF}/CsvBasic-LongDateFormatter", f"sf{SF}/update_streams", f"substitution_parameters-sf{SF}"):
            os.makedirs(os.path.join(data, sub))
        os.makedirs(os.path.join(engine, f"data/ldbc/sf{SF}"))
        script = "set -eu\n" + "\n".join(lines) + "\n"
        for _ in range(2):
            subprocess.run(["bash", "-c", script], cwd=engine, check=True, env=dict(os.environ, LDBC_SNB_DATA_DIR=data))
        return sorted(os.path.relpath(os.path.join(p, n), data) for p, ds, fs in os.walk(data) for n in ds + fs
                      if os.path.islink(os.path.join(p, n)))


def main():
    bad, n = [], 0
    planted = []
    for db in DBS:
        lines = recipe_ln_lines(db)
        n += len(lines)
        if not lines:
            bad.append(f"engines/{db}/justfile: no ln lines found in ldbc-download")
            continue
        inside = links_after_two_runs(lines)
        if inside:
            bad.append(f"engines/{db}: after two runs, links inside the data dir: {inside}")
        planted += links_after_two_runs([l.replace("ln -sfn", "ln -sf") for l in lines])
    caught = len(planted) > 0
    if not caught:
        bad.append("the planted lines without -n left no link inside the data dir: the check cannot fail")
    for b in bad:
        print("FAIL", b)
    print(f"check_engine_ldbc_links {'FAIL' if bad else 'OK'}: {n} ln lines in {len(DBS)} ldbc-download recipes, run "
          f"twice; the planted lines without -n: {'caught' if caught else 'NOT caught'} ({len(planted)} links inside)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
