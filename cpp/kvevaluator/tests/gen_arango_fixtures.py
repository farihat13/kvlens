"""Generate tests/fixtures/arango.json: VPack-mirror documents and packed strings as PYTHON
produces them (formats/arangodb.py _vp_encode_obj / _vp_encode_str), so tests/test_arango.cc
can check the C++ codec byte-for-byte.

    python cpp/kvevaluator/tests/gen_arango_fixtures.py      (from the repo root, in its venv)
"""
import json
import os
import sys

from kvevaluator.formats import arangodb as ar

docs = [
    {"_key": "00100000000003a5", "firstName": "Ann", "lastName": "Li", "gender": "female",
     "birthday": 1, "creationDate": 1354060800000, "locationIP": "1.2.3.4",
     "browserUsed": "Firefox", "email": ["a@x", "b@y"], "language": ["en", "zh"]},
    {"_key": "5010000000000000", "creationDate": 11, "locationIP": "", "browserUsed": "IE",
     "language": "uz", "content": "About Ärger, a short post", "length": 25},
    {"_from": "Person/00100000000003a5", "_to": "Post/5010000000000000", "creationDate": 123},
    {"_from": "Person/00100000000003a5", "_to": "Person/0010000000000001"},
    {"_key": "0060000000000001", "name": "t", "small": -6, "nine": 9, "neg": -300, "big": 1 << 40,
     "n": None, "t": True, "f": False, "longstr": "x" * 200},
    {},
]
strs = ["00100000000003a5", "Person/00100000000003a5", "not-a-vid", "Ann", "x" * 200,
        "Label/notavid", "00100000000003A5"]
fx = {"docs": [{"doc": d, "hex": ar._vp_encode_obj(d).hex()} for d in docs],
      "strs": [{"s": s, "hex": ar._vp_encode_str(s).hex()} for s in strs]}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "arango.json")
json.dump(fx, open(out, "w"), indent=1, ensure_ascii=False)
print(f"wrote {out}: docs={len(docs)} strs={len(strs)}")
