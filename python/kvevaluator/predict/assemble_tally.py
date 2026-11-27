"""Assemble a meta layout's tally (kvevaluator tally: keys and bytes per store part, chunks, locality) from two tallies
of other layouts, with no pass over the records: the props parts from one, the adjacency parts from the other.

A tally's rows are per store part: a `vertex_props` group row, a `props:<label>` chunk family and a locality row whose
target family is `props:<label>` depend only on the spec's vertex_props block; an `adjacency` group row, an
`<etype>:<dir>` chunk family and a locality row over such a family depend only on its adjacency block; an `index` group
row on neither (it must be the same in both inputs). So the tally of (props P, adjacency A) is P's props rows plus A's
adjacency rows. experiments/predict_statistics/check_assembled_tallies.py checks this against direct tallies.

Refused: inputs from other record streams, other binaries, other schemas' specs (index, direction or version blocks
that differ), index rows that differ, a part this tool does not know.

    python -m kvevaluator.predict.assemble_tally --props <tally of a layout with P's props> \\
        --adjacency <tally of a layout with A's adjacency> > <the assembled tally>
"""
import argparse
import hashlib
import json
import os
import sys

PROPS_PART, ADJ_PART, INDEX_PART = "vertex_props", "adjacency", "index"
SPEC_BLOCK = {PROPS_PART: "vertex_props", ADJ_PART: "adjacency"}
GROUP_FIELDS = ("part", "type", "direction", "owner", "detail")
LOCALITY_KEY = ("etype", "direction", "owner_label", "target_label", "family")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def is_props_family(fam):
    """a chunk family of vertex props ("props:<label>"); every other family ("<etype>:<dir>") is adjacency's"""
    return fam.startswith("props:")


def refuse(msg):
    raise SystemExit(f"assemble_tally: {msg}: refused")


def assemble(props, adj, props_path="", adj_path=""):
    """the tally of props' vertex_props block with adj's adjacency block (both tallies as parsed JSON)"""
    for t, path in ((props, props_path), (adj, adj_path)):
        if t.get("format") != "meta":
            refuse(f"{path}: format {t.get('format')!r}, not meta")
    if not same_records(props["records"], adj["records"]):
        refuse(f"other record streams ({props['records']} vs {adj['records']})")
    pv, av = props.get("tally_version"), adj.get("tally_version")
    pb, ab = props["provenance"].get("binary_git_sha"), adj["provenance"].get("binary_git_sha")
    if pv is not None or av is not None:   # versioned: one tally version, whatever binaries made them
        if pv != av:
            refuse(f"other tally versions ({pv} vs {av})")
    elif pb != ab or props["provenance"].get("binary_dirty") != adj["provenance"].get("binary_dirty"):
        # no tally version to compare: warned, both binaries recorded in the provenance (assembled_from)
        from kvevaluator import stale
        stale.note("binaries_differ", f"the tallies {props_path or '(props)'} and {adj_path or '(adjacency)'} were made by other binaries ({pb} dirty "
                   f"{props['provenance'].get('binary_dirty')} vs {ab} dirty {adj['provenance'].get('binary_dirty')}) and "
                   f"carry no tally version")
    sp, sa = props["spec"], adj["spec"]
    shared = (set(sp) | set(sa)) - set(SPEC_BLOCK.values())
    for k in sorted(shared):
        if sp.get(k) != sa.get(k):
            refuse(f"the specs differ outside vertex_props and adjacency (block {k!r})")
    spec = {k: (sa[k] if k == "adjacency" else sp[k]) for k in sp}   # P's key order, A's adjacency block

    groups = []
    index_p = {tuple(g[f] for f in GROUP_FIELDS): g for g in props["groups"] if g["part"] == INDEX_PART}
    index_a = {tuple(g[f] for f in GROUP_FIELDS): g for g in adj["groups"] if g["part"] == INDEX_PART}
    if index_p != index_a:
        differ = sorted(k for k in set(index_p) | set(index_a) if index_p.get(k) != index_a.get(k))
        refuse(f"the index rows differ ({len(differ)} groups, e.g. {differ[:2]})")
    for t, part in ((props, PROPS_PART), (adj, ADJ_PART)):
        for g in t["groups"]:
            if g["part"] not in (PROPS_PART, ADJ_PART, INDEX_PART):
                refuse(f"a group of part {g['part']!r} (known: {PROPS_PART}, {ADJ_PART}, {INDEX_PART})")
            if g["part"] == part:
                groups.append(g)
    groups += list(index_p.values())
    groups.sort(key=lambda g: tuple(g[f] for f in GROUP_FIELDS))   # the tally's own order (a std::map on the key)
    payload = {("payload_bytes" in g) for g in groups}
    if len(payload) > 1:
        refuse("one input carries payload bytes and the other does not")

    chunks = {f: c for f, c in props["chunks"].items() if is_props_family(f)}
    chunks.update({f: c for f, c in adj["chunks"].items() if not is_props_family(f)})
    chunks = dict(sorted(chunks.items()))
    locality = ([r for r in props["locality"] if is_props_family(r["family"])]
                + [r for r in adj["locality"] if not is_props_family(r["family"])])
    # the tally's order: by (etype, direction, owner label, target label), the props family before the adjacency ones
    locality.sort(key=lambda r: (r["etype"], r["direction"], r["owner_label"], r["target_label"],
                                 not is_props_family(r["family"]), r["family"]))
    totals = {k: sum(g[k] for g in groups) for k in ("keys", "key_bytes", "value_bytes")}
    provenance = dict(props["provenance"])
    provenance["assembled_from"] = {
        "tool": "python -m kvevaluator.predict.assemble_tally",
        "props": {"path": os.path.basename(props_path), "sha256": sha256(props_path) if props_path else "",
                  "binary_git_sha": pb},
        "adjacency": {"path": os.path.basename(adj_path), "sha256": sha256(adj_path) if adj_path else "",
                      "binary_git_sha": ab}}
    out = {"format": "meta"}
    if pv is not None:   # the inputs' one version (checked above), as a direct tally says
        out["tally_version"] = pv
    out.update({"spec": spec, "records": props["records"], "groups": groups, "chunks": chunks, "locality": locality,
                "totals": totals, "wall_s": 0.0, "provenance": provenance})
    # a sampled tally (kvevaluator tally --owner-sample) says so: both inputs from the one sample, its block carried
    if "owner_sample" in props or "owner_sample" in adj:
        if props.get("owner_sample") != adj.get("owner_sample"):
            refuse(f"other owner samples ({str(props.get('owner_sample', 'none'))[:80]} vs "
                   f"{str(adj.get('owner_sample', 'none'))[:80]})")
        out["owner_sample"] = props["owner_sample"]
    return out


def same_records(a, b):
    """two tallies' `records` name one stream (util/record_stream.cc record_stream_mismatch): the line count, then the
    sources when both name them, else the whole freshness key; not the path it was read at (one stream may be linked
    into several runs)"""
    if a.get("lines") != b.get("lines"):
        return False
    if a.get("schema_sha256") and b.get("schema_sha256") and a["schema_sha256"] != b["schema_sha256"]:
        return False
    if a.get("content_sha256") and b.get("content_sha256"):
        return a["content_sha256"] == b["content_sha256"]
    if a.get("sources_sha256") and b.get("sources_sha256"):
        return a["sources_sha256"] == b["sources_sha256"]
    if a.get("key_sha256") or b.get("key_sha256"):
        return a.get("key_sha256") == b.get("key_sha256")
    return a == b


def comparable(t):
    """a tally without what differs run to run (its wall time, its provenance): what two equal tallies share"""
    return {k: v for k, v in t.items() if k not in ("wall_s", "provenance")}


def differences(a, b):
    """the rows two tallies differ in, in words ([] when equal outside wall time and provenance)"""
    out = []
    ca, cb = comparable(a), comparable(b)
    for k in sorted(set(ca) | set(cb)):
        if k in ("groups", "locality"):
            key = GROUP_FIELDS if k == "groups" else LOCALITY_KEY
            ra = {tuple(r[f] for f in key): r for r in ca.get(k, [])}
            rb = {tuple(r[f] for f in key): r for r in cb.get(k, [])}
            for x in sorted(set(ra) | set(rb)):
                if ra.get(x) != rb.get(x):
                    out.append(f"{k} {x}: {ra.get(x)} vs {rb.get(x)}")
            if not out and ca.get(k) != cb.get(k):
                out.append(f"{k}: the same rows in another order")
        elif k == "records":                     # one stream read at two paths is one stream
            if not same_records(ca.get(k) or {}, cb.get(k) or {}):
                out.append(f"records: {ca.get(k)} vs {cb.get(k)}")
        elif k == "chunks":
            fa, fb = ca.get(k, {}), cb.get(k, {})
            out += [f"chunks {f}: {fa.get(f)} vs {fb.get(f)}" for f in sorted(set(fa) | set(fb)) if fa.get(f) != fb.get(f)]
        elif ca.get(k) != cb.get(k):
            out.append(f"{k}: {ca.get(k)} vs {cb.get(k)}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--props", required=True, help="a tally whose vertex_props block the layout takes")
    ap.add_argument("--adjacency", required=True, help="a tally whose adjacency block the layout takes")
    a = ap.parse_args(argv)
    with open(a.props) as f:
        props = json.load(f)
    with open(a.adjacency) as f:
        adj = json.load(f)
    out = assemble(props, adj, a.props, a.adjacency)
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
