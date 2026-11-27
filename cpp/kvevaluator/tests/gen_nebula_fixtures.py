"""Generate tests/fixtures/nebula.json: RowV2 rows as PYTHON produces them (formats/nebula.py
_rowv2_encode), so tests/test_nebula.cc can check the C++ codec byte-for-byte.

    python cpp/kvevaluator/tests/gen_nebula_fixtures.py      (from the repo root, in its venv)
"""
import json
import os
import random
import sys

from kvevaluator.formats.nebula import NebulaFormat, NebulaSizing
from kvevaluator.schema.vids import without_key_id
from kvevaluator.datasets.ldbc.schema import get_schema

rnd = random.Random(11)
schema = get_schema("canonical")
cases = []
for ts in (0, 8):
    fmt = NebulaFormat(schema, NebulaSizing(row_timestamp=ts))
    rows = [("Person", {"id": 5, "firstName": "Ann", "lastName": "Li", "gender": "female", "birthday": 1, "creationDate": 2,
                        "locationIP": "1.2.3.4", "browserUsed": "Firefox", "email": ["a@x", "b@y"], "language": ["en"]}),
            ("Person", {"id": 1 << 40, "firstName": "Zoë", "lastName": "Ærø", "gender": "male", "birthday": -86400000,
                        "creationDate": 1354060800000, "locationIP": "", "browserUsed": "IE", "email": [], "language": ["zh", "en"]}),
            ("Person", {"id": 7, "firstName": "N", "gender": "x", "birthday": 0, "creationDate": 3, "locationIP": "9", "browserUsed": "B"}),
            ("Post", {"id": 0, "imageFile": "photo1.jpg", "creationDate": 10, "locationIP": "a", "browserUsed": "b", "length": 0}),
            ("Post", {"id": 3, "creationDate": 11, "locationIP": "a", "browserUsed": "b", "language": "uz",
                      "content": "About Ärger, a short post", "length": 25}),
            ("Tag", {"id": 9, "name": "t"}),
            ("Forum", {"id": 1, "title": "f", "creationDate": 5})]
    for label, props in rows:
        props = without_key_id(props)   # the stored row: id is the key, never a column
        cols = fmt._row_columns(("0000000000000001", label))
        cases.append({"kind": "vertex", "label": label, "cols": [list(c) for c in cols], "props": props,
                      "timestamp": ts, "hex": fmt.encode_value(("0000000000000001", label), props).hex()})
    for etype, props in (("knows", {"creationDate": 123}), ("knows", {}), ("hasCreator", {}), ("studyAt", {"classYear": 2010}),
                         ("workAt", {"workFrom": 1999}), ("hasMember", {"joinDate": 42}), ("likes", {"creationDate": 7})):
        cols = fmt._row_columns(("a", "+" + etype, "0", "b"))
        cases.append({"kind": "edge", "etype": etype, "cols": [list(c) for c in cols], "props": props,
                      "timestamp": ts, "hex": fmt.encode_value(("a", "+" + etype, "0", "b"), props).hex()})
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "nebula.json")
json.dump(cases, open(out, "w"), indent=1, ensure_ascii=False)
print(f"wrote {out}: {len(cases)} cases")
