"""schema.json -- the neutral schema artifact the C++ evaluator reads.

Emitted from a compiled `Schema` plus the vid tag table. Vertices are listed in DECLARATION
order, which is load-bearing: pertype_csr tries a Message vid's candidate labels in that
order (Post before Comment), and that sets the SEEK count of every unknown-type read.
Label / edge-type / index ids are assigned here so a key can be decoded by either side.

Write one: python3 -m kvevaluator.datasets write-schema <dataset> [design] [--out path]
"""
import hashlib
import json

from kvevaluator.schema import vids

# Fixed width (bytes) of the key slot a str value index uses -- Nebula's FIXED_STRING rule.
# A value longer than the width is an ingest error (never truncated: an equality prefix scan
# must not collide). LDBC sf30 maxima: firstName 36, Tag.name 82, TagClass.name 26, Place.name 90.
DEFAULT_INDEX_STRING_WIDTH = 64
INDEX_STRING_WIDTHS = {
    ("Person", "firstName"): 40,
    ("Tag", "name"): 96,
    ("TagClass", "name"): 32,
    ("Place", "name"): 96,
}


def to_json(schema, name="canonical", widths=None):
    widths = {**INDEX_STRING_WIDTHS, **(widths or {})}
    vertices, label_ids = [], {}
    for i, (label, cols) in enumerate(schema.vertex_prop_types.items()):
        group = schema.group_of(label)
        props = [{"name": p, "type": t,
                  "multi": (label, p) in schema.multival_cols,
                  "nullable": (label, p) in schema.nullable_cols} for p, t in cols]
        vertices.append({"label": label, "id": i, "group": group,
                         "tag": vids._TAG[vids.group_of(label)], "props": props})
        label_ids[label] = i
    edges = []
    for i, etype in enumerate(sorted(schema.etypes)):
        edges.append({"etype": etype, "id": i, "reverse": etype in schema.reverse_etypes,
                      "endpoints": [list(p) for p in schema.edge_endpoints.get(etype, ())],
                      "props": [{"name": p, "type": t} for p, t in schema.edge_prop_types.get(etype, ())]})
    # one "type" entry per label (the empty-tag membership index id), then that label's
    # value / sorted property indexes in name order
    indexes, k = [], 0
    for label in schema.vertex_prop_types:
        indexes.append({"label": label, "prop": "", "kind": "type", "id": k}); k += 1
        for p in sorted(p for (l, p) in (schema.indexed_cols | schema.date_cols) if l == label):
            if (label, p) in schema.indexed_cols:
                typ = dict(schema.vertex_prop_types[label])[p]
                entry = {"label": label, "prop": p, "kind": "value", "id": k}
                if typ == "str":
                    entry["width"] = widths.get((label, p), DEFAULT_INDEX_STRING_WIDTH)
            else:
                entry = {"label": label, "prop": p, "kind": "sorted", "id": k}
            # the opt-in index-keying flags (Schema.type_scoped_index / key_sorted_index /
            # value_sorted_index): emitted only when set, so every design without them keeps a
            # byte-identical artifact. The C++ dgraph reads them (its index is predicate-global
            # day-token postings unless told otherwise); the other formats are per-type anyway.
            for flag, cols in (("type_scoped", schema.type_scoped_index),
                               ("key_sorted", schema.key_sorted_index),
                               ("value_sorted", schema.value_sorted_index)):
                if (label, p) in cols:
                    entry[flag] = True
            indexes.append(entry); k += 1
    # vid_tags carries ONLY this schema's own labels' tags, not the global _TAG: a schema
    # artifact (and its hash) must NOT change when an unrelated workload adds a label to the vid
    # tag table. (Before linkbench, _TAG held only LDBC tags, so this reproduces those artifacts.)
    schema_groups = {vids.group_of(l) for l in schema.vertex_prop_types}
    out = {"name": name, "vertices": vertices, "edges": edges, "indexes": indexes,
           "type_indexed_labels": sorted(schema.type_indexed_labels),
           "type_groups": {g: list(m) for g, m in schema.type_groups.items()},
           "vid_tags": {g: t for g, t in vids._TAG.items() if g in schema_groups}}
    if schema.predicates_per_label:      # inside the hash: it changes a Dgraph store's keys. Written only when set,
        out["predicates"] = "per_label"  # so every schema without the line keeps a byte-identical artifact
    out["hash"] = hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()
    # The physical adjacency directives (declared direction included), exported for the C++
    # side's feature-schema ingest. Deliberately AFTER the hash: the hash names the dataset (stats.json
    # records it in its provenance), and these directives change what a STORE holds, never what the dataset contains -- store
    # staleness is the store fingerprint's job, which already hashes the compiled Schema's adj
    # fields. Hashing them here would stale every stats bank for a non-dataset change.
    dated = set(schema.adj_sort) | set(schema.adj_inline) | set(schema.adj_sort_facet)
    out["adj"] = {"sort": dict(schema.adj_sort), "inline": dict(schema.adj_inline),
                  "sort_facet": dict(schema.adj_sort_facet),
                  "dir": {e: schema.adj_direction(e) for e in sorted(dated)}}
    out["prop_groups"] = {g: sorted(m) for g, m in schema.prop_groups.items()}
    return out
