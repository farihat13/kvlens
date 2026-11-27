"""Generate tests/fixtures/dgraph.json: the tagged prim codec (kvstore/valcodec.py), UidPack
postings and dgraph's adjacency / index posting values as PYTHON produces them, so
tests/test_dgraph.cc can check the C++ codecs byte-for-byte.

    python cpp/kvevaluator/tests/gen_dgraph_fixtures.py      (from the repo root, in its venv)
"""
import json
import os
import random
import sys

from kvevaluator.formats.dgraph import DgraphFormat, uidpack_encode
from kvevaluator.kvstore import valcodec
from kvevaluator.datasets.ldbc.schema import get_schema

rnd = random.Random(5)
fx = {"prim": [], "uidpack": [], "adjacency": [], "posting": []}
prims = [None, True, False, 0, -1, 1 << 40, -(1 << 62), "", "Zoë Ærø", ["a", "b"], [], {"creationDate": 123},
         {}, {"k": "v", "n": 7, "l": ["x"]}, [1, "two", None, [3]]]
for v in prims:
    fx["prim"].append({"value": v, "hex": valcodec.encode_leaf(v).hex()})
lists = [[17, 23, 31, 44, 300, 305], [5], list(range(1000, 1600)), [1, 1 << 32, (1 << 32) + 5, 1 << 33],
         sorted(rnd.sample(range(1, 1 << 20), 700)), [(1 << 62) + k * 3 for k in range(10)]]
for uids in lists:
    fx["uidpack"].append({"uids": uids, "hex": uidpack_encode(uids).hex()})
fmt = DgraphFormat(get_schema("canonical"))
h = lambda u: format(u, "016x")
adjs = [[(7, {"creationDate": 5}), (300, {"creationDate": 9})], [(1, {})], [(k * 5, {}) for k in range(1, 400)],
        [(2, {"joinDate": 1}), (3, {"joinDate": 2}), (1 << 40, {"joinDate": 3})]]
for postings in adjs:
    raw = fmt.encode_value(("knows", "D", h(1)), [[h(d), f] for d, f in postings])
    fx["adjacency"].append({"postings": [[d, f] for d, f in postings], "hex": raw.hex()})
for uids in lists:
    raw = fmt.encode_value(("firstName", "I", "x"), [h(u) for u in uids])
    fx["posting"].append({"uids": uids, "hex": raw.hex()})
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "dgraph.json")
json.dump(fx, open(out, "w"), indent=1, ensure_ascii=False)
print(f"wrote {out}: " + ", ".join(f"{k}={len(v)}" for k, v in fx.items()))
