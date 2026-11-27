#!/usr/bin/env python3
"""The layout search's mix cost cache size (search.cache_size) while other processes write the same cache: an entry being
written (<key>.tmp<pid>.npz) is not counted, and a file renamed away between the listing and its size is skipped, not
an error. A fake cache dir, no data; under a second.

    python3 tests/unit/check_cost_cache_size.py

Cases:
  1. complete   two complete entries and one being written: 2 files, the two entries' bytes
  2. race       the listing names a file that is gone by the time its size is read (a parallel chain renamed its
                .tmp onto the entry): no FileNotFoundError, the file is not counted
  3. off        no cache dir: (0, 0)
Exit 1 when a case goes the wrong way. On the code before this check (getsize on every listed file), case 2 raised
FileNotFoundError, as run-layout-choice.sh did once at SF3 with parallel side chains."""
import glob
import os
import sys
import tempfile

from kvcost.layout_search import search as S

FAILED = []


def case(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
    if not ok:
        FAILED.append(name)


def write(path, n):
    with open(path, "wb") as f:
        f.write(b"x" * n)


def main():
    with tempfile.TemporaryDirectory() as d:
        S.set_cache(d)
        write(os.path.join(d, "a" * 32 + ".npz"), 100)
        write(os.path.join(d, "b" * 32 + ".npz"), 50)
        write(os.path.join(d, "c" * 32 + ".npz" + f"{S.TMP_MARK}123.npz"), 7)     # being written
        got = S.cache_size()
        case("complete: entries counted, the one being written not", got == (2, 150), f"{got}")

        gone = os.path.join(d, "d" * 32 + ".npz")         # listed, then renamed away before its size is read
        real_glob = glob.glob
        S.glob.glob = lambda pattern, **kw: real_glob(pattern, **kw) + [gone]
        try:
            got = S.cache_size()
            case("race: a file gone after the listing is skipped", got == (2, 150), f"{got}")
        except FileNotFoundError as e:
            case("race: a file gone after the listing is skipped", False, f"raised {e!r}")
        finally:
            S.glob.glob = real_glob
    S.set_cache(None)
    case("off: no cache dir", S.cache_size() == (0, 0))
    print(f"check_cost_cache_size: {len(FAILED)} failed" + (f" ({', '.join(FAILED)})" if FAILED else ", all cases ok"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
