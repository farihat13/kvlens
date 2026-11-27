#!/usr/bin/env python3
"""LDBC SNB's fetch step (its manifest's fetch_module; `just fetch-dataset ldbc=<sf>`): download LDBC SNB Interactive v1
data for one scale factor from LDBC's host, checked against inputs/data-sources/ldbc/archives.json.

Per SF, three archives: the CSVs (CsvBasic-LongDateFormatter), the substitution parameters, and the update streams
(numpart-1, read by the live engines). LDBC hosts no substitution parameters for sf0.3 and sf1: the manifest marks
those "hosted": false and a fetch skips them (the artifact's own parameters are under inputs/params/ldbc). Each archive is checked by its size and sha256 as it downloads; after extraction,
every file is checked by its size and sha256 too, and a file the manifest does not name is refused. A tree already in
place is checked, not downloaded again; one that differs from the manifest is refused and left alone (nothing in the
data dir is overwritten). Stdlib only (plus tar and unzstd on PATH), so it also runs by its file before the Python
environment exists; the dir is kvevaluator.paths.datasets_dir()/ldbc, loaded from its file.

The fetch step's four calls (kvevaluator.datasets.fetch): plan(scale), missing(scale), ensure(scale), check(scale).
By its file, for a mirror or a new SF:

    python3 python/kvevaluator/datasets/ldbc/fetch.py 0.1 3 --check   # verify what is in place; downloads nothing
    python3 python/kvevaluator/datasets/ldbc/fetch.py 30 --host file:///<dir>   # from a mirror laid out as the host
    python3 python/kvevaluator/datasets/ldbc/fetch.py 10 --pin         # add an SF to the manifest from what the host serves now
"""
import argparse, hashlib, importlib.util, json, os, shutil, subprocess, sys, time, urllib.error

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 4))   # python/kvevaluator/datasets/ldbc/ -> the repo root
MANIFEST = os.path.join(REPO, "inputs", "data-sources", "ldbc", "archives.json")
EXPECTED = os.path.join(REPO, "inputs", "data-sources", "ldbc", "expected.json")   # per SF: a fetch's recorded time
HOST = "https://datasets.ldbcouncil.org"
HINT = ("Get the archive by other means and point --host file:///<dir> at it, laid out as on the host "
        "(inputs/data-sources/ldbc/README.md)")


def load(name, path):
    """a module loaded from its file (stdlib only; no install needed)"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DL = load("download", os.path.join(REPO, "run", "download.py"))    # the pinned download with retries, shared with fetch_results


def kvevaluator_paths():
    """kvevaluator.paths, loaded from its file (stdlib only; no install needed): the one reader of .env and the environment"""
    return load("kvevaluator_paths", os.path.join(REPO, "python", "kvevaluator", "paths.py"))


def archives_for(sf, host=HOST):
    """the three archives of one SF: (name, URL, where it goes under <data dir>/datasets/ldbc)"""
    return [("csv", f"{host}/snb-interactive-v1/social_network-sf{sf}-CsvBasic-LongDateFormatter.tar.zst",
             f"sf{sf}/CsvBasic-LongDateFormatter"),
            ("params", f"{host}/snb-interactive-v1/substitution_parameters-sf{sf}.tar.zst",
             f"substitution_parameters-sf{sf}"),
            ("updates", f"{host}/snb-interactive-v1-updates/social_network-sf{sf}-numpart-1.tar.zst",
             f"sf{sf}/update_streams/social_network-sf{sf}-numpart-1")]


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tree_files(root):
    """{relative path: [size, sha256]} of every file under root; a symlink or a special file is refused"""
    out = {}
    for dirpath, dirnames, names in os.walk(root):
        dirnames.sort()
        for n in sorted(names + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
            p = os.path.join(dirpath, n)
            if os.path.islink(p) or not os.path.isfile(p):
                raise ValueError(f"{p} is a symlink or not a regular file")
            out[os.path.relpath(p, root)] = [os.path.getsize(p), sha256_of(p)]
    return out


def tree_problems(root, want):
    """[problem] of the tree at root against the pinned {path: [size, sha256]}: missing, extra or different files"""
    try:
        have = tree_files(root)
    except ValueError as e:
        return [str(e)]
    probs = [f"missing {p}" for p in sorted(set(want) - set(have))]
    probs += [f"not in the manifest: {p}" for p in sorted(set(have) - set(want))]
    probs += [f"differs: {p} (size {have[p][0]}, want {want[p][0]})" for p in sorted(set(want) & set(have))
              if have[p] != want[p]]
    return probs


def extract(archive, into):
    """the archive's one top dir, extracted under `into` (a new, empty dir); tar refuses absolute and .. member names"""
    names = subprocess.run(["tar", "-t", "--use-compress-program=unzstd", "-f", archive], check=True,
                           capture_output=True, text=True).stdout.split()
    tops = {n.split("/")[0] for n in names if n not in (".", "./")}
    if len(tops) != 1:
        raise ValueError(f"the archive has {len(tops)} top-level entries, want one dir: {sorted(tops)[:5]}")
    subprocess.run(["tar", "-x", "--use-compress-program=unzstd", "--no-same-owner", "--no-same-permissions",
                    "-f", archive, "-C", into], check=True)
    top = os.path.join(into, tops.pop())
    if os.path.islink(top) or not os.path.isdir(top):
        raise ValueError(f"the archive's top entry {top} is not a dir")
    return top


def fetch_sfs(sfs, check=False, pin=False, only=None, manifest=MANIFEST, host=HOST, retry_wait=30):
    """each SF's archives (only: a set of csv, params, updates), fetched when missing or (check) verified; [problem]"""
    missing = [t for t in ("tar", "unzstd") if shutil.which(t) is None]
    if missing and not check:
        sys.exit(f"[ldbc] missing: {', '.join(missing)} (LDBC's archives are .tar.zst): install with the Needs block in README.md")
    paths = kvevaluator_paths()
    try:
        ldbc = os.path.join(str(paths.datasets_dir()), "ldbc")
    except paths.PathsError as e:
        sys.exit(f"[ldbc] {e}")
    m = json.load(open(manifest)) if os.path.exists(manifest) else {"version": 1, "sfs": {}}
    only = set(only) if only else {"csv", "params", "updates"}
    bad = []
    for sf in sfs:
        bad_before = len(bad)
        if pin and sf in m["sfs"]:
            sys.exit(f"[ldbc] sf{sf} is already in {manifest}: an entry is never rewritten")
        if not pin and sf not in m["sfs"]:
            sys.exit(f"[ldbc] sf{sf} is not in {manifest} (pinned: {', '.join(sorted(m['sfs']))}); "
                     f"`--pin` adds it")
        pinned = {} if pin else m["sfs"][sf]
        canon = {name: url for name, url, _ in archives_for(sf)}   # a pin records LDBC's own URL, also when fetched from a mirror
        for name, url, rel in archives_for(sf, host.rstrip("/")):
            if name not in only:
                continue
            dest = os.path.join(ldbc, rel)
            if not pin:
                if os.path.basename(pinned[name]["url"]) != os.path.basename(url):
                    sys.exit(f"[ldbc] sf{sf} {name}: the manifest pins {pinned[name]['url']}, not {url}")
                if not pinned[name]["hosted"]:
                    print(f"[ldbc] sf{sf} {name}: not hosted by LDBC (pinned as such); skipped", flush=True)
                    continue
                want = pinned[name]["files"]
            if os.path.exists(dest) and not pin:
                t0 = time.monotonic()
                probs = tree_problems(dest, want)
                print(f"[ldbc] sf{sf} {name}: {dest} in place, {len(want)} files checked by sha256: "
                      f"{'OK' if not probs else f'{len(probs)} problems'} ({time.monotonic() - t0:.0f} s)", flush=True)
                bad += [f"sf{sf} {name}: {p}" for p in probs[:20]]
                continue
            if check or (pin and os.path.exists(dest)):
                print(f"[ldbc] sf{sf} {name}: {dest} " + ("missing" if check else "exists; pin into an empty data dir"))
                bad.append(f"sf{sf} {name}: {dest} " + ("missing" if check else "exists"))
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            work = os.path.join(ldbc, f".fetch-{os.getpid()}")
            os.makedirs(work)
            t0 = time.monotonic()
            try:
                archive = os.path.join(work, os.path.basename(url))
                print(f"[ldbc] sf{sf} {name}: downloading {url}", flush=True)
                if pin:
                    try:
                        size, sha = DL.download_retrying(url, archive, retry_wait, "ldbc-fetch", HINT)
                    except urllib.error.HTTPError as e:
                        if e.code != 404:
                            raise
                        m["sfs"].setdefault(sf, {})[name] = {"url": canon[name], "hosted": False}
                        print(f"[ldbc] sf{sf} {name}: the host has no such archive (404); pinned as not hosted")
                        continue
                else:
                    size, sha = DL.download_retrying(url, archive, retry_wait, "ldbc-fetch", HINT,
                                                     want_size=pinned[name]["size"], want_sha=pinned[name]["sha256"])
                os.makedirs(os.path.join(work, "x"))
                top = extract(archive, os.path.join(work, "x"))
                if pin:
                    files = tree_files(top)
                    m["sfs"].setdefault(sf, {})[name] = {"url": canon[name], "hosted": True, "size": size, "sha256": sha,
                                                         "files": files}
                else:
                    probs = tree_problems(top, want)
                    if probs:
                        bad += [f"sf{sf} {name}: {p}" for p in probs[:20]]
                        print(f"[ldbc] sf{sf} {name}: the extracted tree has {len(probs)} problems; nothing moved")
                        continue
                os.rename(top, dest)
                print(f"[ldbc] sf{sf} {name}: {size / 1e6:.1f} MB archive, sha256 OK, "
                      f"{len(files if pin else want)} files OK -> {dest} ({time.monotonic() - t0:.0f} s)", flush=True)
            except (OSError, ValueError, subprocess.CalledProcessError) as e:
                bad.append(f"sf{sf} {name}: {e}")
                print(f"[ldbc] sf{sf} {name}: REFUSED: {e}", flush=True)
            finally:
                shutil.rmtree(work)                        # this run's own download and leftover extraction only
        if pin and len(bad) > bad_before:
            m["sfs"].pop(sf, None)                         # an SF is pinned whole or not at all
        elif pin:
            tmp = manifest + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(m, fh, indent=1, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, manifest)
            print(f"[ldbc] sf{sf} pinned in {manifest}")
    for b in bad:
        print(f"  BAD {b}")
    return bad


def _hosted(scale):
    """[(name, its pin, where it goes under <data dir>/datasets/ldbc)] of the SF's archives LDBC hosts"""
    m = json.load(open(MANIFEST))
    if scale not in m["sfs"]:
        sys.exit(f"[ldbc] sf{scale} is not in {MANIFEST} (pinned: {', '.join(sorted(m['sfs']))})")
    return [(name, m["sfs"][scale][name], rel) for name, _, rel in archives_for(scale) if m["sfs"][scale][name]["hosted"]]


def plan(scale):
    """what a fetch of the SF would do, from the pins: {present, to_fetch (archive names), download_bytes, disk_bytes,
    seconds (the recorded time of a whole fetch, or None), date}; nothing is hashed"""
    ldbc = os.path.join(str(kvevaluator_paths().datasets_dir()), "ldbc")
    arch = _hosted(scale)
    gone = [(n, p) for n, p, rel in arch if not os.path.exists(os.path.join(ldbc, rel))]
    rec = (json.load(open(EXPECTED)) if os.path.exists(EXPECTED) else {}).get("sfs", {}).get(scale, {})
    return {"present": [n for n, _, _ in arch if n not in dict(gone)], "to_fetch": [n for n, _ in gone],
            "download_bytes": sum(p["size"] for _, p in gone),
            "disk_bytes": sum(f[0] for _, p in gone for f in p["files"].values()),
            "seconds": rec.get("seconds") if len(gone) == len(arch) else None, "date": rec.get("date")}


def missing(scale):
    """[what is not in place] of the SF (existence only, nothing hashed): what a run that reads the data needs fetched"""
    ldbc = os.path.join(str(kvevaluator_paths().datasets_dir()), "ldbc")
    return [os.path.join(ldbc, rel) for _, _, rel in _hosted(scale) if not os.path.exists(os.path.join(ldbc, rel))]


def ensure(scale):
    """each of the SF's hosted archives fetched when missing, every file in place checked; [problem]"""
    return fetch_sfs([scale])


def check(scale):
    """every file of the SF's hosted archives in place, checked by size and sha256 against the pins; downloads nothing;
    ([problem], files checked)"""
    bad = fetch_sfs([scale], check=True)
    ldbc = os.path.join(str(kvevaluator_paths().datasets_dir()), "ldbc")
    return bad, sum(len(p["files"]) for _, p, rel in _hosted(scale) if os.path.exists(os.path.join(ldbc, rel)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sf", nargs="+", help="scale factors, e.g. 0.1 3")
    ap.add_argument("--check", action="store_true", help="verify the trees in place; download nothing")
    ap.add_argument("--pin", action="store_true", help="download an SF not in the manifest yet and add it")
    ap.add_argument("--only", default=None, help="a comma list of csv, params, updates (default: all three)")
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--host", default=HOST, help="a mirror of LDBC's host (or file:///dir); each archive is still checked")
    ap.add_argument("--retry-wait", type=float, default=30, help=f"seconds between the {DL.ATTEMPTS} attempts at one archive")
    a = ap.parse_args()
    if a.pin and a.only:
        sys.exit("[ldbc] --pin pins all three archives of an SF; drop --only")
    bad = fetch_sfs(a.sf, check=a.check, pin=a.pin, only=a.only.split(",") if a.only else None, manifest=a.manifest,
                    host=a.host, retry_wait=a.retry_wait)
    print(f"ldbc fetch {'OK' if not bad else 'FAIL'}: sf {', '.join(a.sf)}" + (f", {len(bad)} problems" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
