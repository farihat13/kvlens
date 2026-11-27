"""Each engines/<db>/build.sh checks its needs (engines/check-tools.sh reads engines/needs.tsv) before it patches or
builds anything: the check comes before the first patch step in all three scripts, and Dgraph's build run on a PATH
without curl exits 1 with "missing: curl" and the setup command, and applies no patch. ArangoDB's build run with PYTHON
set to a Python without distutils (the repo venv's 3.12, which fails inside ICU 64's configure) exits 1 with
"missing: python3-setuptools", and applies no patch; that run has stub tools on its PATH, so a machine without
ArangoDB's (optional) build tools reaches the check too. The needs file itself: every row has five columns, a known
`how` and a known check, every database has build rows, and engines/setup.sh --dry-run reads them all.

    python tests/unit/check_engine_build_tools.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT

DBS = ("dgraph", "nebulagraph", "arangodb")      # built from source: a check, then patches
NEEDS_DBS = DBS + ("tugraph",)                   # every database with needs; TuGraph's release is fetched, no patch


def order_problems():
    """the check_tools line must come before the script's first 'Applying' line"""
    out = []
    for db in DBS:
        lines = (ROOT / "engines" / db / "build.sh").read_text().splitlines()
        check = next((i for i, l in enumerate(lines) if l.startswith(f"check_tools {db} build")), None)
        patch = next((i for i, l in enumerate(lines) if "=== Applying" in l), None)
        if check is None or patch is None or check > patch:
            out.append(f"engines/{db}/build.sh: check_tools at line {check}, first patch step at line {patch}")
    return out


CHECK_ALONE = 'source engines/check-tools.sh\ncheck_tools "$1" "$2"\necho "every need met"\n'


def run_without(script, tool, *args):
    """run `script` (with args) on a PATH that has every program of this one except `tool` -> (rc, output)"""
    with tempfile.TemporaryDirectory() as d:
        for dirname in os.environ.get("PATH", "").split(os.pathsep):
            if not os.path.isdir(dirname):
                continue
            for name in os.listdir(dirname):
                if name != tool and not os.path.lexists(os.path.join(d, name)):
                    os.symlink(os.path.join(dirname, name), os.path.join(d, name))
        out = subprocess.run(["bash", script, *args], cwd=ROOT, capture_output=True, text=True, env=dict(os.environ, PATH=d),
                             timeout=60)
    return out.returncode, out.stdout + out.stderr


def check_alone():
    """the check itself (engines/check-tools.sh check_tools), without a build script around it, so it runs whether or
    not a database is built here: Dgraph's build needs on a PATH without curl must stop with "missing: curl" and the
    setup command; on the full PATH the call must not name curl -> a problem line, or None"""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", dir=ROOT / "engines", delete=False) as f:
        f.write(CHECK_ALONE)
    try:
        rel = os.path.relpath(f.name, ROOT)
        rc, out = run_without(rel, "curl", "dgraph", "build")
        full = subprocess.run(["bash", rel, "dgraph", "build"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(f.name)
    if rc != 1 or "missing: curl" not in out or "bash engines/setup.sh --db dgraph" not in out or "every need met" in out:
        return f"check_tools dgraph build without curl: rc {rc}, output {out.strip()[-300:]!r}"
    if "missing: curl" in full.stdout + full.stderr:
        return f"check_tools dgraph build on the full PATH names curl: {(full.stdout + full.stderr).strip()[-300:]!r}"
    return None


def arangodb_build_with_stub_tools(python):
    """run arangodb/build.sh with PYTHON=`python`, ArangoDB's tools stubbed and the OpenSSL header a stand-in: only the
    Python gap can stop it, on a machine with or without those tools"""
    with tempfile.TemporaryDirectory() as d:
        for tool in ("clang-17", "clang++-17", "llvm-ar-17", "cmake", "make", "git", "patch", "dpkg-query"):
            if tool.endswith("-17") or tool == "dpkg-query" or shutil.which(tool) is None:    # the rest where missing
                stub = os.path.join(d, tool)
                with open(stub, "w") as f:      # exit 0 also "links" the empty C++ program; every package "installed"
                    f.write("#!/bin/sh\n" + ("echo 'install ok installed'\n" if tool == "dpkg-query" else "") + "exit 0\n")
                os.chmod(stub, 0o755)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""), PYTHON=python)
        out = subprocess.run(["bash", "engines/arangodb/build.sh"], cwd=ROOT, capture_output=True, text=True, env=env,
                             timeout=60)
    return out.returncode, out.stdout + out.stderr


def needs_problems():
    """engines/needs.tsv: five tab-separated columns, a known when, how and check; build rows for every database"""
    out, seen = [], set()
    for n, line in enumerate((ROOT / "engines" / "needs.tsv").read_text().splitlines(), 1):
        if line.startswith("#") or not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) != 5:
            out.append(f"engines/needs.tsv:{n}: {len(cols)} columns, want 5")
            continue
        db, when, how, what, check = cols
        seen.add((db, when))
        if (db not in NEEDS_DBS or when not in ("build", "run", "profile") or how not in ("apt", "go", "service", "main")
                or not (check == "pkg" or check.startswith(("cmd:", "sh:")))):
            out.append(f"engines/needs.tsv:{n}: unknown db, when, how or check in {cols}")
    out += [f"engines/needs.tsv: no build row for {db}" for db in NEEDS_DBS if (db, "build") not in seen]
    return out, len(seen)


def main():
    bad = order_problems()
    more, groups = needs_problems()
    bad += more
    alone = check_alone()
    bad += [alone] if alone else []
    dry = subprocess.run(["bash", "engines/setup.sh", "--dry-run"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    lines = [l for l in dry.stdout.splitlines() if l.startswith("  ")]
    if dry.returncode != 0 or not lines or "setup-engines (dgraph nebula arangodb)" not in dry.stdout:
        bad.append(f"engines/setup.sh --dry-run: rc {dry.returncode}, {len(lines)} need lines, output {dry.stdout[-300:]!r} {dry.stderr[-300:]!r}")
    if (ROOT / "engines/dgraph/bin/dgraph-kvstats").exists():
        print("NOTE engines/dgraph/bin/dgraph-kvstats exists: build.sh skips before its check; the planted run is skipped")
        planted = "skipped"
    else:
        rc, out = run_without("engines/dgraph/build.sh", "curl")
        ok = rc == 1 and "missing: curl" in out and "bash engines/setup.sh --db dgraph" in out and "Applying" not in out
        planted = "caught" if ok else "NOT caught"
        if not ok:
            bad.append(f"dgraph/build.sh without curl: rc {rc}, output {out.strip()[-300:]!r}")
    no_distutils = os.path.join(sys.prefix, "bin", "python3")      # a venv's 3.12 has no distutils (setuptools is not in it)
    if (ROOT / "engines/arangodb/bin/arangod").exists():
        print("NOTE engines/arangodb/bin/arangod exists: build.sh skips before its check; the planted run is skipped")
        icu = "skipped"
    elif subprocess.run([no_distutils, "-c", "import distutils"], capture_output=True).returncode == 0:
        print(f"NOTE {no_distutils} imports distutils: no Python without it to plant; the planted run is skipped")
        icu = "skipped"
    else:
        rc, text = arangodb_build_with_stub_tools(no_distutils)
        ok = rc == 1 and "missing: python3-setuptools" in text and "Applying" not in text
        icu = "caught" if ok else "NOT caught"
        if not ok:
            bad.append(f"arangodb/build.sh with PYTHON={no_distutils}: rc {rc}, output {text.strip()[-300:]!r}")
    for b in bad:
        print("FAIL", b)
    print(f"check_engine_build_tools {'FAIL' if bad else 'OK'}: the check comes first in {len(DBS)} build scripts; "
          f"needs.tsv: {groups} (database, moment) groups, setup.sh --dry-run read {len(lines)} needs; "
          f"the check alone without curl: {'NOT caught' if alone else 'caught'}; "
          f"dgraph's build without curl: {planted}; arangodb's build with a Python lacking distutils: {icu}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
