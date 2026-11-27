"""Generate tests/fixtures/chunks.json: chunk bodies + size estimates as PYTHON produces them
(formats/pertype_csr.py), so tests/test_chunks.cc can check the C++ codecs byte-for-byte and
the chunker's logical estimate (a contract: it decides chunk boundaries, hence S/G/N).

    python cpp/kvevaluator/tests/gen_chunk_fixtures.py      (from the repo root, in its venv)
"""
import json
import os
import sys

from kvevaluator.formats import pertype_csr as pc
from kvevaluator.schema.vids import without_key_id
from kvevaluator.datasets.ldbc.schema import get_schema

schema = get_schema("canonical")
fmt = pc.PerTypeCsrFormat(schema)

cases = []
# props chunk: Person rows with multi + an absent nullable-ish cell, non-ASCII text
person_cols = fmt._vcols("Person")
rows = [(5, {"id": 5, "firstName": "Ann", "lastName": "Li", "gender": "female", "birthday": 1, "creationDate": 2,
             "locationIP": "1.2.3.4", "browserUsed": "Firefox", "email": ["a@x", "b@y"], "language": ["en"]}),
        (1 << 40, {"id": 1 << 40, "firstName": "Zoë", "lastName": "Ærø", "gender": "male", "birthday": -86400000,
                   "creationDate": 1354060800000, "locationIP": "", "browserUsed": "IE", "email": [], "language": ["zh", "en"]}),
        ((1 << 40) + 7, {"id": (1 << 40) + 7, "firstName": "N", "gender": "x", "birthday": 0, "creationDate": 3,
                         "locationIP": "9", "browserUsed": "B"})]        # lastName / email / language absent
cases.append({"kind": "props", "label": "Person", "cols": [list(c) for c in person_cols],
              "rows": [[u, p] for u, p in rows],
              "hex": fmt._serialize_props(person_cols, rows).hex(),
              "est": [8 + pc._est_props(without_key_id(p)) for _u, p in rows]})
# Post rows: nullable content / imageFile
post_cols = fmt._vcols("Post")
prows = [(1 << 60, {"id": 0, "imageFile": "photo1.jpg", "creationDate": 10, "locationIP": "a", "browserUsed": "b", "length": 0}),
         ((1 << 60) + 3, {"id": 3, "creationDate": 11, "locationIP": "a", "browserUsed": "b", "language": "uz",
                          "content": "About Ärger, a short post", "length": 25})]
cases.append({"kind": "props", "label": "Post", "cols": [list(c) for c in post_cols],
              "rows": [[u, p] for u, p in prows],
              "hex": fmt._serialize_props(post_cols, prows).hex(),
              "est": [8 + pc._est_props(without_key_id(p)) for _u, p in prows]})
# adjacency without ordinals (hasCreator) and with ordinals (knows)
groups = [(5, [7, 300, 1 << 41], None), (6, [1], None), (1 << 40, [2, 3, 4, 5, 6], None)]
cases.append({"kind": "adj", "with_ordinals": False, "groups": [[s, ds, o] for s, ds, o in groups],
              "hex": fmt._serialize_adj(groups, False).hex(), "est": [9 + 5 * len(ds) for _s, ds, _o in groups]})
groups_o = [(5, [7, 300, 1 << 41], [0, 1, 2]), (6, [1], [3]), (1 << 40, [2, 3, 4, 5, 6], [4, 5, 6, 7, 300000])]
cases.append({"kind": "adj", "with_ordinals": True, "groups": [[s, ds, o] for s, ds, o in groups_o],
              "hex": fmt._serialize_adj(groups_o, True).hex(), "est": [9 + 8 * len(ds) for _s, ds, _o in groups_o]})
# edge props (knows.creationDate) keyed by ordinal
ecols = fmt._ecols("knows")
erows = [(0, {"creationDate": 123}), (2, {"creationDate": 456}), (300000, {})]
cases.append({"kind": "ep", "etype": "knows", "cols": [list(c) for c in ecols], "rows": [[o, p] for o, p in erows],
              "hex": fmt._serialize_eprops(ecols, erows).hex(), "est": [2 + pc._est_props(p) for _o, p in erows]})

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "chunks.json")
json.dump(cases, open(out, "w"), indent=1, ensure_ascii=False)
print(f"wrote {out}: {len(cases)} cases")
