#!/usr/bin/env python3
"""kvevaluator predict refuses a statistics file built from another schema or scale (no store, seconds): the statistics'
schema_hash and the anchor file's must equal the schema's dataset hash, and the statistics' provenance.sf the scale of
each request's store (in --accuracy also the opened store's manifest), or predict exits 1 naming the file.
Planted: another hash, no hash, another scale; the control (the right one) passes the check.

  1. --plans, stats.json with another schema_hash: refused, the file named
  2. --plans, stats.json with no provenance: refused ("none recorded")
  3. --plans, the right hash: not refused
  4. --plans, an anchor file with another schema_hash: refused, the anchor file named
  5. --plans, an anchor file with no schema_hash: refused
  6. --plans, both right: not refused
  7. --plans, a request whose store is at scale 3, stats.json built at 0.1: refused, the scale named
  8. --plans, the request at 0.1: not refused
  9. --plans, an anchor file of another graph (another record stream's line count: another scale): refused
The statistics contract (inputs/contract/stats.schema.json), on a complete synthetic file over the LDBC schema:
 10. each field the contract requires, left out: DatasetStats::load refuses it by name
 11. each field the contract does not require, left out: load does not refuse (the two lists agree both ways)
 12. an IS3 request (a knows hop) with distinct_src lacking knows: refused by name (at load, the file naming knows
     elsewhere; or where it is read), never a default of 1; the complete file: not refused
 13. an IC1 request (read_index_eq on Person.firstName) with ndv lacking Person|firstName: refused by name
 14. python -m kvevaluator.predict.check_stats: the complete file passes (contract and schema); each required field
     left out, a label left out of type_counts, an edge type left out of distinct_dst: each refused by name
 15. (retired with the Python reader: covered by cases 10 and 13)
 16. --check-doc: a doc whose block is the rendered tables passes; one changed cell fails
 17. load: a label the file names (an end label) with no type_counts entry, an edge type with edges but no esrc key,
     and sizes without content, each refused by name
 18. check_stats --schema: a property of the schema with no sizes.content row refused by name
 19. a hop from many rows over an edge type whose end labels count 0 vertices refused (no population), not priced
     at the participants' mean
 20. a sampled sizes (sizes.sample, the share b was measured on): 0.01 and 1 load and pass check_stats; 0 and 1.5
     are refused by both

    python3 tests/unit/check_predict_stats_schema.py
"""
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT, cpp_bin
from kvevaluator.datasets import schema_path
from kvevaluator.predict import check_stats

SCHEMA = schema_path("ldbc")
fails = cases = 0


def check(cond, what):
    global fails, cases
    cases += 1
    print(("PASS " if cond else "FAIL ") + what)
    fails += 0 if cond else 1


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)
    return path


def complete_stats(schema_hash):
    """a synthetic statistics file with every field the LDBC plans read (no data needed)"""
    ds = json.load(open(SCHEMA))
    labels = [v["label"] for v in ds["vertices"]]
    etypes = {e["etype"]: e for e in ds["edges"]}
    content = {}
    for v in ds["vertices"]:
        for p in v["props"]:
            content[f"prop|{v['label']}|{p['name']}"] = {"b": 80, "u": 10}
    for e, spec in etypes.items():
        content[f"eprop|{e}"] = {"b": 0, "u": 20}
        for s_, d_ in spec["endpoints"]:
            content[f"esrc|{e}|{s_}"] = {"b": 0, "u": 1}
            content[f"edst|{e}|{d_}"] = {"b": 0, "u": 1}
    for ix in ds["indexes"]:
        if ix["prop"]:
            content[f"tok|{ix['label']}|{ix['prop']}"] = {"b": 80, "u": 10}
    out = {"type_counts": {l: 10 for l in labels}, "edge_totals": {e: 20 for e in etypes},
           "distinct_src": {e: 5 for e in etypes}, "distinct_dst": {e: 5 for e in etypes}, "cond_degree": {},
           "ndv": {"Person|firstName": 5, "Person|lastName": 5, "Place|name": 5, "Tag|name": 5, "TagClass|name": 5,
                   "Organisation|name": 5},
           # one value listed per indexed property (not IC1's "Ian": its rows are the rest, so ndv is read too)
           "top_values": {k: {"k": 10, "holders": 10, "values": [["x", 2]]}
                          for k in ("Person|firstName", "Person|lastName", "Place|name", "Tag|name", "TagClass|name",
                                    "Organisation|name")},
           "date_days": {"Post|creationDate": {"first_day": 15000, "counts": [3, 0, 7]},
                         "Comment|creationDate": {"first_day": 15001, "counts": [4, 6]}},
           "date_axis": {"labels": ["Post", "Comment"], "prop": "creationDate"}, "forest": {}, "recency": {},
           # the ranked walks over a hop's far ends: ic2 / ic9 / is2 (hasCreator|in), ic8 (replyOf|in); 1 is the flat form
           "burstiness": {"hasCreator|in|creationDate": 1, "replyOf|in|creationDate": 1},
           # agrees with the counts above (load checks it): 10 Persons, 20 knows edges each way; both = out + in
           "degree_hist": {"Person|knows|out": [[0, 2, 0, 0], [3, 8, 20, 60]], "Person|knows|in": [[0, 2, 0, 0], [3, 8, 20, 60]],
                           "Person|knows|both": [[0, 2, 0, 0], [4, 8, 40, 240]]},
           "dataset": "ldbc", "records": {"path": "r.jsonl", "lines": 100},
           "sizes": {"content": content}, "provenance": {"sf": "0.1", "schema_hash": schema_hash}}
    # every int property the schema declares, null (no element holds it: no int_values entry needed)
    out["value_range"] = {f"prop|{v['label']}|{p['name']}": None for v in ds["vertices"] for p in v["props"]
                          if p.get("type") == "int"}
    out["value_range"].update({f"eprop|{e}|{p['name']}": None for e, spec in etypes.items()
                               for p in spec.get("props") or [] if p.get("type") == "int"})
    # every declared end of an edge type and indexed property of that label: 5 owners with 20 edges, one value listed
    indexed = {}
    for k in out["top_values"]:
        lab, prop = k.split("|")
        indexed.setdefault(lab, []).append(prop)
    out["top_degrees"] = {}
    for e, spec in etypes.items():
        for s_, d_ in spec["endpoints"]:
            for lab, d in ((s_, "out"), (d_, "in")):
                for prop in indexed.get(lab, []):
                    out["top_degrees"][f"{lab}|{e}|{d}|{prop}"] = {"k": 10, "owners": 5, "owner_values": 5, "edges": 20,
                                                                  "values": [["x", 2]]}
    if schema_hash is not None:
        out["schema_hash"] = schema_hash
    return out


def request(path, query, params):
    from kvevaluator.measure import server, workload_plans
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ir = workload_plans.ir_for("interactive", query)
    req = server.build_request(ir, params, os.path.join(os.path.dirname(path), "stores", "ldbc", "sf0.1", "meta-x"),
                                  fmt="meta")
    return write(path, req)


def run(args):
    r = subprocess.run([str(cpp_bin()), "predict", *args], capture_output=True, text=True)
    return r.returncode, r.stderr


def main():
    good = json.load(open(SCHEMA))["hash"]
    other = "0" * 64
    with tempfile.TemporaryDirectory() as t:
        def stats(name, hash_=None, prov=True):
            d = complete_stats(hash_)   # complete: the refusal checked is the schema's, not a missing field's
            if not prov:
                del d["provenance"]
                d.pop("schema_hash", None)
            return write(os.path.join(t, name), d)

        def graph(name, hash_=None, lines=100):
            d = {"anchor_degrees": {}, "anchor_recency": {}, "records": {"path": "r.jsonl", "lines": lines}}
            if hash_ is not None:
                d["schema_hash"] = hash_
            return write(os.path.join(t, name), d)

        req = write(os.path.join(t, "req.json"), {})
        os.makedirs(os.path.join(t, "tally"))
        # --plans reads a tally per store before it estimates: an empty one (the tally of an empty record stream) for
        # the requests' store, meta-x; the cases check the statistics' refusals, not the costs
        open(os.path.join(t, "empty.jsonl"), "w").close()
        with open(os.path.join(t, "tally", "meta-x.json"), "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", SCHEMA, "--spec",
                            os.path.join(str(ROOT), "inputs", "specs", "grid", "props_split_adj_split.json"),
                            "--records", os.path.join(t, "empty.jsonl")], stdout=f, check=True)
        with open(os.path.join(t, "tally", "meta-x.json")) as f:     # the fixture names one record stream: the
            tally = json.load(f)                                       # statistics' (predict refuses two)
        tally["records"] = {"path": "r.jsonl", "lines": 100}
        with open(os.path.join(t, "tally", "meta-x.json"), "w") as f:
            json.dump(tally, f)
        g_good = graph("g_good.json", good)

        def costed(s, r=req):
            """predict --plans on statistics file s (the anchor file right), one request r"""
            return run(["--plans", "--out-predicted", tempfile.mkdtemp(prefix="cap-", dir=t), "--stats", s,
                        "--anchor-stats", g_good, "--tally-dir", os.path.join(t, "tally"), "--schema", SCHEMA, r])

        rc, err = costed(stats("other.json", other))
        check(rc == 1 and "refused" in err and "other.json" in err, f"1 another schema_hash refused (exit {rc})")
        rc, err = costed(stats("noprov.json", prov=False))
        check(rc == 1 and "refused" in err and "none recorded" in err, f"2 no provenance refused (exit {rc})")
        rc, err = costed(stats("good.json", good))
        check("refused" not in err, f"3 the right hash passes the check (exit {rc}: the empty request fails after it)")

        plans = lambda g: run(["--plans", "--out-predicted", os.path.join(t, "cap"),
                               "--stats", os.path.join(t, "good.json"), "--anchor-stats", g,
                               "--tally-dir", os.path.join(t, "tally"), "--schema", SCHEMA, req])
        rc, err = plans(graph("g_other.json", other))
        check(rc == 1 and "refused" in err and "g_other.json" in err, f"4 an anchor file of another schema refused (exit {rc})")
        rc, err = plans(graph("g_none.json"))
        check(rc == 1 and "refused" in err and "none recorded" in err, f"5 an anchor file with no hash refused (exit {rc})")
        rc, err = plans(g_good)
        check("refused" not in err, f"6 both right pass the check (exit {rc})")
        req3 = write(os.path.join(t, "req3.json"), {"store": {"scale": "3"}})
        req01 = write(os.path.join(t, "req01.json"), {"store": {"scale": "0.1"}})
        good_stats = os.path.join(t, "good.json")
        rc, err = costed(good_stats, req3)
        check(rc == 1 and "refused" in err and "scale '0.1', not '3'" in err, f"7 a request at another scale refused (exit {rc})")
        rc, err = costed(good_stats, req01)
        check("refused" not in err, f"8 the request at the stats' scale passes the check (exit {rc})")
        rc, err = plans(graph("g_sf3.json", good, lines=61977656))
        check(rc == 1 and "built from different graphs" in err, f"9 an anchor file of another graph refused (exit {rc})")
        contract_cases(t, good, costed)
    print(f"{cases} cases, {fails} failed")
    if cases == 0:
        return 1
    return 1 if fails else 0


def contract_cases(t, good, card):
    """card(stats file, request=an empty one): predict --plans' exit code and stderr"""
    contract = check_stats.load_contract()
    required = contract["required"]
    full = complete_stats(good)
    n_req = 0
    for f in required:
        d = dict(full)
        del d[f]
        rc, err = card(write(os.path.join(t, f"no_{f}.json"), d))
        n_req += rc == 1 and f"have no '{f}'" in err
    check(n_req == len(required) > 0, f"10 each required field left out is refused by name ({n_req} of {len(required)})")
    optional = [f for f in contract["properties"] if f not in required and f in full and f != "provenance"]
    n_opt = 0
    for f in optional:
        d = dict(full)
        del d[f]
        rc, err = card(write(os.path.join(t, f"no_{f}.json"), d))
        n_opt += "have no '" not in err
    check(n_opt == len(optional) > 0, f"11 each optional field left out is not refused at load ({n_opt} of {len(optional)})")

    is3 = request(os.path.join(t, "reqs", "x__is3__1.json"), "is3", {"person": "933"})
    d = json.loads(json.dumps(full))
    del d["distinct_src"]["knows"]
    rc, err = card(write(os.path.join(t, "no_knows_src.json"), d), is3)
    # refused at load (the file names knows elsewhere, so its missing count is seen there) or, failing that, where read
    named = "distinct_src entry for 'knows'" in err or ("edge type 'knows'" in err and "distinct_src" in err)
    check(rc != 0 and named, f"12 IS3 with distinct_src lacking knows: refused by name (exit {rc})")
    rc, err = card(write(os.path.join(t, "full.json"), full), is3)
    check("distinct_src entry for 'knows'" not in err and "have no '" not in err,
          f"12 IS3 on the complete file: not refused for knows (exit {rc})")
    ic1 = request(os.path.join(t, "reqs", "x__ic1__1.json"), "ic1", {"person": "933", "firstName": "Ian"})
    d = json.loads(json.dumps(full))
    del d["ndv"]["Person|firstName"]
    rc, err = card(write(os.path.join(t, "no_ndv_firstname.json"), d), ic1)
    check(rc != 0 and "ndv entry for 'Person|firstName'" in err, f"13 IC1 with ndv lacking Person|firstName: refused by name (exit {rc})")
    rc, err = card(os.path.join(t, "full.json"), ic1)
    check("ndv entry for 'Person|firstName'" not in err and "have no '" not in err,
          f"13 IC1 on the complete file: not refused for Person|firstName (exit {rc})")
    d = json.loads(json.dumps(full))
    del d["top_values"]["Person|firstName"]
    rc, err = card(write(os.path.join(t, "no_top_values_firstname.json"), d), ic1)
    check(rc != 0 and "top_values entry for 'Person|firstName'" in err,
          f"13b IC1 with top_values lacking Person|firstName: refused by name (exit {rc})")
    d = json.loads(json.dumps(full))
    d["top_values"]["Person|firstName"]["values"] = [["x", 11]]
    rc, err = card(write(os.path.join(t, "top_values_over.json"), d), ic1)
    check(rc != 0 and "top_values" in err and "above its holders" in err,
          f"13c top_values counting more than its holders: refused at load (exit {rc})")

    tag_hop = [{"name": "tag", "op": "read_index_eq", "label": "Tag", "prop": "name", "value": "x"},
               {"name": "msgs", "op": "read_neighbors", "etype": "hasTag", "dir": "in", "srcs": {"ref": "tag"}}]
    entry = write(os.path.join(t, "reqs", "x__taghop__1.json"),
                  {"params": {}, "run": {"P": 1}, "store": {"dir": os.path.join(t, "meta-x"), "format": "meta",
                                                            "scale": "0.1", "schema": SCHEMA},
                   "plan": {"plan": "taghop", "params": {}, "let": {}, "plans": {"b": {"output": "msgs", "steps": tag_hop}},
                            "bodies": {"b": {"sha": "000000000000", "sources": ["b"]}}}})
    d = json.loads(json.dumps(full))
    del d["top_degrees"]["Tag|hasTag|in|name"]
    rc, err = card(write(os.path.join(t, "no_top_degrees_tag.json"), d), entry)
    check(rc != 0 and "top_degrees entry for 'Tag|hasTag|in|name'" in err,
          f"13d a hasTag hop from a Tag name read with top_degrees lacking Tag|hasTag|in|name: refused by name (exit {rc})")
    rc, err = card(os.path.join(t, "full.json"), entry)
    check("top_degrees" not in err, f"13d the complete file estimates the hop (exit {rc})")
    d = json.loads(json.dumps(full))
    d["top_degrees"]["Tag|hasTag|in|name"]["values"] = [["x", 21]]
    rc, err = card(write(os.path.join(t, "top_degrees_over.json"), d), entry)
    check(rc != 0 and "top_degrees" in err and "above its edges" in err,
          f"13e top_degrees summing above its edges: refused at load (exit {rc})")

    ds = json.load(open(SCHEMA))
    ok = not check_stats.check_contract(full, full["provenance"], contract) and not check_stats.check_schema(full, None, ds)
    check(ok, "14 check_stats: the complete file passes the contract and the schema")
    named = sum(any(r.startswith(f"{f}:") for r in check_stats.check_contract({k: v for k, v in full.items() if k != f},
                                                                               full["provenance"], contract))
                for f in required)
    check(named == len(required), f"14 check_stats: each required field left out refused by name ({named} of {len(required)})")
    d = json.loads(json.dumps(full))
    del d["type_counts"]["Forum"]
    del d["distinct_dst"]["likes"]
    r = check_stats.check_schema(d, None, ds)
    check(any(x.startswith("type_counts.Forum:") for x in r) and any(x.startswith("distinct_dst.likes:") for x in r),
          f"14 check_stats: a label and an edge type left out refused by name ({len(r)} refused)")
    d = dict(full)
    d["stray"] = 1
    check(any(x.startswith("stray:") for x in check_stats.check_contract(d, full["provenance"], contract)),
          "14 check_stats: a field the contract does not know is refused")

    # 15 (retired with the Python reader): a file without ndv is case 10 (ndv is a required field, refused at load by
    # name); a missing ndv entry refused by name is case 13
    d = json.loads(json.dumps(full))
    del d["type_counts"]["Forum"]          # Forum is an end label of hasMember, hasModerator, containerOf, hasTag
    rc, err = card(write(os.path.join(t, "named_label.json"), d))
    check(rc == 1 and "name the label 'Forum'" in err, f"17 a named label with no type_counts entry refused by name (exit {rc})")
    d = json.loads(json.dumps(full))
    for k in [k for k in d["sizes"]["content"] if k.startswith("esrc|likes|")]:
        del d["sizes"]["content"][k]
    rc, err = card(write(os.path.join(t, "no_esrc.json"), d))
    check(rc == 1 and "esrc|likes|<label>" in err, f"17 an edge type with edges and no esrc key refused by name (exit {rc})")
    rc, err = card(write(os.path.join(t, "no_content.json"), {**full, "sizes": {}}))
    check(rc == 1 and "sizes.content" in err, f"17 sizes without content refused by name (exit {rc})")
    d = json.loads(json.dumps(full))
    del d["sizes"]["content"]["prop|Person|gender"]
    r = check_stats.check_schema(d, None, ds)
    check(any(x.startswith("sizes.content.prop|Person|gender:") for x in r), f"18 check_stats: a missing property row refused by name ({len(r)} refused)")
    for share, ok in ((0.01, True), (1, True), (0, False), (1.5, False)):
        d = json.loads(json.dumps(full))
        d["sizes"]["sample"] = share
        rc, err = card(write(os.path.join(t, f"sample_{share}.json"), d))
        py = check_stats.check_contract(d, full["provenance"], contract)
        loaded = "sizes.sample" not in err
        check(loaded == ok and (not py) == ok,
              f"20 sizes.sample {share}: {'loads and passes check_stats' if ok else 'refused by load and check_stats'} "
              f"(exit {rc}; check_stats {py[:1]})")
    # a knows hop from many rows (posts' creators) prices its sources by the population degree
    hops = [{"name": "posts", "op": "read_vertices_by_label", "label": "Post"},
            {"name": "who", "op": "read_neighbors", "etype": "hasCreator", "dir": "out", "srcs": {"ref": "posts"}},
            {"name": "friends", "op": "read_neighbors", "etype": "knows", "dir": "out", "srcs": {"ref": "who"}}]
    fof = write(os.path.join(t, "reqs", "x__hops__1.json"),
                {"params": {}, "run": {"P": 1}, "store": {"dir": os.path.join(t, "meta-x"), "format": "meta",
                                                          "scale": "0.1", "schema": SCHEMA},
                 "plan": {"plan": "hops", "params": {}, "let": {}, "plans": {"b": {"output": "friends", "steps": hops}},
                          "bodies": {"b": {"sha": "000000000000", "sources": ["b"]}}}})
    d = json.loads(json.dumps(full))
    d["type_counts"]["Person"] = 0
    del d["degree_hist"]   # its Person rows would disagree with the count at load; the case is the hop's refusal
    for k in [k for k in d["top_values"] if k.startswith("Person|")]:
        del d["top_values"][k]   # its holders would be above the count at load, too
    for k in [k for k in d["top_degrees"] if k.startswith("Person|")]:
        del d["top_degrees"][k]   # its owners would be above the count at load, too
    rc, err = card(write(os.path.join(t, "no_person.json"), d), fof)
    check(rc != 0 and "no esrc population" in err, f"19 a hop over knows with no Person vertices refused by name (exit {rc})")
    rc, err = card(os.path.join(t, "full.json"), fof)
    check("population" not in err, f"19 the complete file prices the hop (exit {rc})")

    tables = check_stats.render_tables(contract)
    doc = write(os.path.join(t, "doc.md"), "x")
    with open(doc, "w") as f:
        f.write(f"# doc\n\n{check_stats.BEGIN}\n{tables}{check_stats.END}\n")
    good_doc = not check_stats.check_doc(doc, contract)
    with open(doc, "w") as f:
        f.write(f"# doc\n\n{check_stats.BEGIN}\n{tables.replace('vertices of each label', 'vertices')}{check_stats.END}\n")
    check(good_doc and check_stats.check_doc(doc, contract), "16 --check-doc: the rendered block passes, a changed cell fails")


if __name__ == "__main__":
    sys.exit(main())
