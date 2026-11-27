"""run/check-build-tools.sh (the first step of `just build`) checks exactly the apt packages of README.md's Needs block,
and on a PATH without cmake it exits 1 and says "missing: cmake".

    python tests/unit/check_build_tools.py
"""
import os
import re
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT

CHECK = ["bash", "run/check-build-tools.sh"]


def readme_packages(text):
    """the packages of the README's first `sudo apt-get install -y ...` line, the build's (a trailing backslash joins
    the next line); a later install line is the paper scale's tools, not the build's"""
    text = re.sub(r"\\\n\s*", " ", text)
    lines = re.findall(r"sudo apt-get install -y ([^\n`#]*)", text)
    return [set(pkgs.split()) for pkgs in lines[:1]]


def check_without(tool):
    """run the check on a PATH that has every program of this one except `tool` -> (rc, output)"""
    with tempfile.TemporaryDirectory() as d:
        for dirname in os.environ.get("PATH", "").split(os.pathsep):
            if not os.path.isdir(dirname):
                continue
            for name in os.listdir(dirname):
                if name != tool and not os.path.lexists(os.path.join(d, name)):
                    os.symlink(os.path.join(dirname, name), os.path.join(d, name))
        out = subprocess.run(CHECK, cwd=ROOT, capture_output=True, text=True, env=dict(os.environ, PATH=d))
    return out.returncode, out.stdout + out.stderr


def main():
    bad = []
    checked = set(subprocess.run(CHECK + ["--packages"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split())
    readme = readme_packages((ROOT / "README.md").read_text())
    if readme != [checked]:
        bad.append(f"README.md's apt line(s) {[sorted(r) for r in readme]} != the build check's packages {sorted(checked)}")
    rc, out = check_without("cmake")
    if rc != 1 or "missing: cmake" not in out or "Needs block in README.md" not in out:
        bad.append(f"without cmake: rc {rc}, output {out.strip()!r} (wanted rc 1, 'missing: cmake' and the README pointer)")
    for b in bad:
        print("FAIL", b)
    print(f"check_build_tools {'FAIL' if bad else 'OK'}: {len(checked)} packages equal to the README block; "
          f"the planted missing cmake {'caught' if rc == 1 and 'missing: cmake' in out else 'NOT caught'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
