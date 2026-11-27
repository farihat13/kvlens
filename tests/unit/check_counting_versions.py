#!/usr/bin/env python3
"""The counting versions a capture is keyed on come from the binary, and one rule reads them (measure/counting.py).
Store-free; needs the built binary (its `--version` "counting").

  1. the binary states the seam's pair and one pair per format, every format the registry builds included
  2. a kvstats identity written by main before the change (54f2ea9's latency_fresh.identity on an sf0.1 meta store,
     below), read by today's _query_matches against today's identity of the same store: fresh, reads and writes, while
     meta's format version is 1 (it counted under 1); stale, the format version named, after a bump
  3. the same identity with another stored seam version (3), or a writes one without write_measurement_version: stale,
     with the reason named
  4. a record that stores `counting`: fresh when equal, stale when one compared part differs; a part a capture does
     not compare (a read capture's write pair) does not matter
  5. a grid record from before (no `counting`): read as seam 4, format 1; a writes record from before: its
     write_measurement_version
  6. an unknown record kind or format is refused by name

    python3 tests/unit/check_counting_versions.py
"""
import sys

from kvevaluator.formats import FORMATS
from kvevaluator.measure import counting, latency_fresh

# kvstats identities exactly as main (54f2ea9) wrote them, for is1 and iu1 on the sf0.1 meta store meta-5019375411fb
OLD_READS = {"store": {"key_codec": "meta-v4-part-start", "value_codec_hash": "6e5a01b8bf7eaf25", "schema": "canonical",
                       "keys": 4796013}, "nparam": 2, "params": {"is1": "266d7636905d"}, "plans": {"is1": "1e6d50b76a42"},
             "measurement_version": 4, "step_columns": "d82625bbd44b"}
OLD_WRITES = {"store": {"key_codec": "meta-v4-part-start", "value_codec_hash": "6e5a01b8bf7eaf25", "schema": "canonical",
                        "keys": 4796013}, "nparam": 2, "params": {"iu1": "83cc646320f5"}, "plans": {"iu1": "443831f29bcf"},
              "measurement_version": 4, "step_columns": "d82625bbd44b", "write_measurement_version": 1}
SLUG = "meta-5019375411fb"
failed = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
    if not ok:
        failed.append(name)


def today(old, counted):
    """today's identity of the same store and query: the same fields, the stored versions replaced by the stamp"""
    new = {k: v for k, v in old.items() if k not in ("measurement_version", "write_measurement_version")}
    new.update(format="meta", counting=counting.stamp("meta"), counted=counted)
    return new


def ok_run(ident):
    return {"status": "ok", "identity": {SLUG: ident}}


def main():
    c = counting.binary_counting()
    known = set(FORMATS) | {"dgraph_engine", "nebula_engine", "arangodb_engine"}
    case("1 the binary states the seam pair and every format's", set(c["seam"]) == {"read", "write"}
         and known <= set(c["formats"]), f"seam {c['seam']}, formats {sorted(c['formats'])}")

    # main's records were counted under meta's format version 1: fresh while the binary's is 1, stale (the format
    # version named) once a counting change bumps it
    meta_v1 = c["formats"]["meta"]["read"] == 1
    for name, old, q, counted in (("reads", OLD_READS, "is1", "read"), ("writes", OLD_WRITES, "iu1", "read+write")):
        new = today(old, counted)
        why = counting.counting_fresh(old, "meta", counted, "kvstats")[1]
        case(f"2 main's {name} identity: fresh iff meta's format version is 1",
             latency_fresh._query_matches(ok_run(old), SLUG, q, new) is meta_v1
             and (meta_v1 or "format version" in (why or "")), why)
    bad = dict(OLD_READS, measurement_version=3)
    case("3a another stored seam version: stale", not latency_fresh._query_matches(ok_run(bad), SLUG, "is1",
                                                                                  today(OLD_READS, "read")),
         counting.counting_fresh(bad, "meta", "read", "kvstats")[1])
    bad = {k: v for k, v in OLD_WRITES.items() if k != "write_measurement_version"}
    got = counting.counting_fresh(bad, "meta", "write", "kvstats")   # the write parts alone: a read bump is 3a's case
    case("3b a writes identity with no write version: stale, named", got[0] is False and "write_seam" in (got[1] or ""), got[1])

    now = counting.stamp("meta")
    case("4a a stored stamp equal to the binary's: fresh", counting.counting_fresh({"counting": now}, "meta", "read", "grid")[0])
    moved = dict(now, format=now["format"] + 1)
    got = counting.counting_fresh({"counting": moved}, "meta", "read", "grid")
    case("4b the format's read version moved: stale", got[0] is False and "format" in got[1], got[1])
    moved = dict(now, write_seam=now["write_seam"] + 1)
    case("4c the write pair moved, a read capture: fresh", counting.counting_fresh({"counting": moved}, "meta", "read", "grid")[0])
    got = counting.counting_fresh({"counting": moved}, "meta", "write", "grid_writes")
    case("4d the write pair moved, a write capture: stale", got[0] is False, got[1])

    got = counting.counting_fresh({"measurement_version": 7}, "meta", "read", "grid")
    case("5a a grid record from before: seam 4, format 1", got[0] is (now["seam"] == 4 and now["format"] == 1), str(got))
    got = counting.counting_fresh({"measurement_version": 9, "write_measurement_version": 1}, "meta", "write", "grid_writes")
    case("5b a writes record from before: its write_measurement_version", got[0] is (now["write_seam"] == 1), str(got))

    try:
        counting.counting_fresh({}, "meta", "read", "other")
        case("6a an unknown record kind is refused", False, "no error")
    except ValueError as e:
        case("6a an unknown record kind is refused", "unknown record kind" in str(e), str(e))
    try:
        counting.stamp("no_such_format")
        case("6b an unknown format is refused", False, "no error")
    except SystemExit as e:
        case("6b an unknown format is refused", "no_such_format" in str(e), str(e)[:90])
    print(f"check_counting_versions: {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
