"""The databases' needs are written once, in engines/needs.tsv: no file under engines/ (a README, a justfile, a script
other than engines/setup.sh) holds a `sudo apt-get install` line of its own (an install on the machine; a line run
inside NebulaGraph's build container is the image's business), and each engines/<db>/justfile's `setup` recipe calls
engines/setup.sh for its database. A planted `sudo apt-get install` line in a README is caught.

    python tests/unit/check_engine_needs.py
"""
import re
import sys

from kvevaluator.paths import ROOT

E = ROOT / "engines"
DBS = {"dgraph": "dgraph", "nebulagraph": "nebula", "arangodb": "arangodb"}     # dir -> its --db key
OWN = ("README.md", "justfile", "build.sh", "lifecycle.sh", "load-data.sh", "start-server.sh", "get.sh")


def files():
    """the files we write under engines/ (not the databases' fetched sources): the top level's, and each database's own"""
    out = [p for p in sorted(E.iterdir()) if p.is_file() and p.name != "setup.sh"]
    for db in DBS:
        out += [E / db / name for name in OWN if (E / db / name).exists()]
        out += sorted((E / db / "scripts").glob("*.sh")) if (E / db / "scripts").is_dir() else []
    return out


def problems(texts):
    """a second copy of a need: a `sudo apt-get install` outside engines/setup.sh; a setup recipe that does not call it"""
    out = [f"{name}: its own `sudo apt-get install` line: {m.group(0)[:80]}" for name, text in texts.items()
           for m in re.finditer(r"sudo [^\n]*apt-get install[^\n]*", text)]
    for db, key in DBS.items():
        recipe = re.search(r"^setup:.*?(?=^\S|\Z)", texts.get(f"{db}/justfile", ""), re.S | re.M)
        if recipe is None or f"../setup.sh --db {key}" not in recipe.group(0):
            out.append(f"{db}/justfile: its setup recipe does not call ../setup.sh --db {key}")
    return out


def main():
    texts = {str(p.relative_to(E)): p.read_text(errors="replace") for p in files()}
    bad = problems(texts)
    planted = dict(texts, **{"README.md": texts["README.md"] + "\nsudo apt-get install -y libplanted-dev\n"})
    caught = any(x.startswith("README.md:") for x in problems(planted))
    if not caught:
        bad.append("the planted `sudo apt-get install` line in the README was not caught")
    for b in bad:
        print("FAIL", b)
    print(f"check_engine_needs {'FAIL' if bad else 'OK'}: {len(texts)} files under engines/ hold no install line of their "
          f"own, {len(DBS)} setup recipes call engines/setup.sh; the planted line {'caught' if caught else 'NOT caught'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
