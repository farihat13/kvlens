"""Check a statistics file against the contract predict mode reads (inputs/contract/stats.schema.json), before predict.

The file is checked as predict reads it (DatasetStats::load: stats.json, with the anchor tier of --anchor-stats when it
is given). Three checks, each refusing by the field's name:
  1. the contract: every field the schema requires, each field's type, no field the schema does not know;
  2. with --schema (a compiled dataset schema, schema.json): every label of the schema in type_counts, every edge type
     in edge_totals, distinct_src and distinct_dst (0 when the data has none), each edge type's end labels
     (sizes.content esrc|E|L, edst|E|L), and the anchor file built from the same record stream and schema;
  3. with --plans DIR (plan files, *.json): per plan, the fields its steps read; a required one the file lacks is
     refused (the field, the op, the plan file and body named), an optional one is listed with the rule that falls
     back without it.
Exit 0 when nothing is refused, 1 otherwise. --render-tables prints the contract's tables (Markdown) from the schema,
a contract doc's source; --check-doc PATH exits 1 when that doc's
rendered block differs from them.

    python -m kvevaluator.predict.check_stats output/sf3/stats.json --anchor-stats A --schema S --plans DIR
    python -m kvevaluator.predict.check_stats --render-tables
"""
import argparse
import glob
import json
import os
import sys

from kvevaluator.paths import CONTRACT_DIR
from kvevaluator.capture.step_record import PART
from kvevaluator.plans.ir.capabilities import DEFAULT_DIR, KV_READ_OPS

CONTRACT = CONTRACT_DIR / "stats.schema.json"
BEGIN, END = "<!-- rendered from inputs/contract/stats.schema.json: begin -->", "<!-- rendered: end -->"


def load_contract(path=CONTRACT):
    with open(path) as f:
        return json.load(f)


# ---- 1. the contract (the subset of JSON Schema it uses: type, required, properties, additionalProperties, items,
#         minItems, maxItems, enum, minimum) ---------------------------------------------------------------------------

_TYPES = {"object": dict, "array": list, "string": str, "null": type(None)}


def _is(v, t):
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def validate(v, sch, path="", out=None):
    """the places `v` breaks `sch`, as "path: what" lines"""
    out = [] if out is None else out
    types = sch.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        if not any(_is(v, t) for t in types):
            out.append(f"{path or '(file)'}: a {type(v).__name__}, not {' or '.join(types)}")
            return out
    if "enum" in sch and v not in sch["enum"]:
        out.append(f"{path}: {v!r} is not one of {sch['enum']}")
    if "minimum" in sch and _is(v, "number") and v < sch["minimum"]:
        out.append(f"{path}: {v} is below {sch['minimum']}")
    if "exclusiveMinimum" in sch and _is(v, "number") and v <= sch["exclusiveMinimum"]:
        out.append(f"{path}: {v} is not above {sch['exclusiveMinimum']}")
    if "maximum" in sch and _is(v, "number") and v > sch["maximum"]:
        out.append(f"{path}: {v} is above {sch['maximum']}")
    if isinstance(v, dict):
        for k in sch.get("required", []):
            if k not in v:
                out.append(f"{path + '.' if path else ''}{k}: missing (required)")
        props = sch.get("properties", {})
        extra = sch.get("additionalProperties", True)
        for k, x in v.items():
            where = f"{path}.{k}" if path else k
            if k in props:
                validate(x, props[k], where, out)
            elif extra is False:
                out.append(f"{where}: not a field of the contract")
            elif isinstance(extra, dict):
                validate(x, extra, where, out)
    if isinstance(v, list):
        if "minItems" in sch and len(v) < sch["minItems"]:
            out.append(f"{path}: {len(v)} items, fewer than {sch['minItems']}")
        if "maxItems" in sch and len(v) > sch["maxItems"]:
            out.append(f"{path}: {len(v)} items, more than {sch['maxItems']}")
        if "items" in sch:
            for i, x in enumerate(v):
                validate(x, sch["items"], f"{path}[{i}]", out)
    return out


ANCHOR_KEYS = ("anchor_degrees", "anchor_recency", "anchor_far_labels")   # the anchor tier DatasetStats::load takes from --anchor-stats


def merged(stats_path, anchor_path=None):
    """(the statistics with the anchor tier, stats.json's own provenance, the anchor file): DatasetStats::load's view"""
    with open(stats_path) as f:
        d = json.load(f)
    prov = d.get("provenance")
    anchor = None
    if anchor_path:
        with open(anchor_path) as f:
            anchor = json.load(f)
        for k in ANCHOR_KEYS:
            if k in anchor:
                d[k] = anchor[k]
    return d, prov, anchor


def check_contract(d, prov, contract):
    """refusals: the merged statistics against the contract (stats.json's own provenance where load reads it)"""
    view = dict(d)
    if prov is not None:
        view["provenance"] = prov
    return validate(view, contract)


# ---- 1b. tier 2 against tier 1 (DatasetStats::load's check_against_tier1, stats.cc: the same rules) -----------------

def _same(a, b):
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def check_tier1(d):
    """refusals: a tier-2 field that disagrees with the file's tier-1 counts (a hand-written flat form, say)"""
    out = []
    tc, et = d.get("type_counts") or {}, d.get("edge_totals") or {}
    owners = {(o.get("etype"), o.get("direction"), o.get("owner_label")): o for o in d.get("owners") or []}
    per_edge, per_label = {}, {}
    for k, rows in sorted((d.get("degree_hist") or {}).items()):
        parts = k.split("|")
        if len(parts) < 3 or parts[-1] not in ("out", "in", "both"):
            out.append(f"degree_hist '{k}': the key is not label|edge type|direction (out, in or both)")
            continue
        label, etype, dr = parts[0], "|".join(parts[1:-1]), parts[-1]
        if label not in tc or etype not in et:
            out.append(f"degree_hist '{k}': type_counts or edge_totals does not count '{label}' / '{etype}'")
            continue
        if not all(isinstance(r, list) and len(r) == 4 and all(isinstance(x, (int, float)) and x >= 0 for x in r)
                   for r in rows):
            out.append(f"degree_hist '{k}': a row is not [bucket, vertices, sum d, sum d^2] of non-negative numbers")
            continue
        if any(r[0] != int(r[0]) for r in rows):
            out.append(f"degree_hist '{k}': a bucket is not a whole number")
        n = sum(r[1] for r in rows)
        own = sum(r[1] for r in rows if r[0] > 0)
        s1 = sum(r[2] for r in rows if r[0] > 0)
        s2 = sum(r[3] for r in rows if r[0] > 0)
        if not _same(n, tc[label]):
            out.append(f"degree_hist '{k}': its vertices sum to {n:g}, type_counts['{label}'] is {tc[label]:g}")
        if own > 0 and s2 < s1 * s1 / own * (1 - 1e-9):
            out.append(f"degree_hist '{k}': sum d^2 {s2:g} is below (sum d)^2 / owners {s1 * s1 / own:g}")
        per_label[k] = s1
        if dr == "both":
            continue
        per_edge[f"{etype}|{dr}"] = per_edge.get(f"{etype}|{dr}", 0) + s1
        o = owners.get((etype, dr, label))
        if o is not None and not (_same(own, o.get("owners", 0)) and _same(s1, o.get("edges", 0))):
            out.append(f"degree_hist '{k}': its owners and summed degree are {own:g} and {s1:g}, the owners row says "
                       f"{o.get('owners', 0):g} and {o.get('edges', 0):g}")
    for ed, s1 in sorted(per_edge.items()):
        etype = ed.rsplit("|", 1)[0]
        if not _same(s1, et[etype]):
            out.append(f"degree_hist '{ed}': the labels' summed degrees are {s1:g}, edge_totals['{etype}'] is {et[etype]:g}")
    for k, s1 in sorted(per_label.items()):
        if k.endswith("|both"):
            base = k[:-len("|both")]
            if f"{base}|out" not in per_label or f"{base}|in" not in per_label:
                out.append(f"degree_hist '{k}': a 'both' row needs the label's out and in rows")
            elif not _same(s1, per_label[f"{base}|out"] + per_label[f"{base}|in"]):
                out.append(f"degree_hist '{k}': its summed degree is {s1:g}, the label's out and in rows sum to "
                           f"{per_label[f'{base}|out'] + per_label[f'{base}|in']:g}")
    content = (d.get("sizes") or {}).get("content") or {}
    for k, h in sorted((d.get("date_days") or {}).items()):
        if "|" not in k or not isinstance(h, dict) or not isinstance(h.get("counts"), list):
            out.append(f"date_days '{k}': it is not label|property: {{first_day, counts}}")
            continue
        label, prop = k.split("|", 1)
        u = (content.get(f"prop|{label}|{prop}") or {}).get("u")
        if u is None:
            out.append(f"date_days '{k}': sizes.content has no prop|{label}|{prop} to check its total against")
        elif not _same(sum(h["counts"]), u):
            out.append(f"date_days '{k}': its counts sum to {sum(h['counts']):g}, sizes.content has {u:g} vertices "
                       f"with the property")
    for k, v in sorted((d.get("neighbor_degree_ratio") or {}).items()):
        etype, _, dr = k.rpartition("|")
        if dr not in ("out", "in", "both") or etype not in et:
            out.append(f"neighbor_degree_ratio '{k}': the key is not a counted edge type|direction")
        elif not isinstance(v, (int, float)) or v < 1 - 1e-9:
            out.append(f"neighbor_degree_ratio '{k}': {v} is below 1, which no set of degrees has")
    for k, v in sorted((d.get("value_range") or {}).items()):
        kind, _, rest = k.partition("|")
        owner, bar, _ = rest.rpartition("|")
        if kind not in ("prop", "eprop") or not bar or not owner:
            out.append(f"value_range '{k}': the key is not prop|label|property or eprop|edge type|property")
            continue
        if kind == "prop" and owner not in tc:
            out.append(f"value_range '{k}': type_counts has no label '{owner}'")
        if kind == "eprop" and owner not in et:
            out.append(f"value_range '{k}': edge_totals has no edge type '{owner}'")
        if v is not None and not (isinstance(v, list) and len(v) == 2
                                  and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
                                  and v[0] <= v[1]):
            out.append(f"value_range '{k}': it is not [least, greatest] or null")
    vr = d.get("value_range") or {}
    for k, h in sorted((d.get("int_values") or {}).items()):
        # date_days' form per integer value: it spans value_range exactly and counts every holder
        r = vr.get(k)
        if not (isinstance(r, list) and len(r) == 2):
            out.append(f"int_values '{k}': value_range has no [least, greatest] for it")
            continue
        if not (isinstance(h, dict) and isinstance(h.get("first"), (int, float)) and isinstance(h.get("counts"), list)):
            out.append(f"int_values '{k}': it is not {{first, counts}}")
            continue
        if h["first"] != r[0] or len(h["counts"]) != r[1] - r[0] + 1:
            out.append(f"int_values '{k}': its values do not run from value_range's least {r[0]:g} to its greatest {r[1]:g}")
            continue
        if not all(isinstance(c, (int, float)) and not isinstance(c, bool) and c >= 0 for c in h["counts"]):
            out.append(f"int_values '{k}': a count is negative or not a number")
            continue
        n = sum(h["counts"])
        owner = k.split("|")[1]
        if k.startswith("prop|"):
            u = (content.get(k) or {}).get("u")
            if u is not None and not _same(n, u):
                out.append(f"int_values '{k}': its counts sum to {n:g}, sizes.content {k} has {u:g} vertices with the property")
        elif n > et.get(owner, 0) * (1 + 1e-9):
            out.append(f"int_values '{k}': its counts sum to {n:g}, above edge_totals['{owner}']")
    for k, v in sorted((d.get("burstiness") or {}).items()):
        parts = k.rsplit("|", 2)
        if len(parts) != 3 or parts[1] not in ("out", "in") or parts[0] not in et:
            out.append(f"burstiness '{k}': the key is not a counted edge type|out or in|property")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            out.append(f"burstiness '{k}': {v} is not a number >= 0")
    per, by_owner = {}, {}
    for k, v in sorted((d.get("degree_by_far_label") or {}).items()):
        parts = k.split("|")
        if len(parts) != 4 or parts[2] not in ("out", "in") or parts[1] not in et:
            out.append(f"degree_by_far_label '{k}': the key is not owner label|a counted edge type|out or in|far label")
            continue
        if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                                 for x in v) and 0 <= v[0] <= v[1]):
            out.append(f"degree_by_far_label '{k}': it is not [edges, summed owner degree] with the sum >= the edges")
            continue
        per[(parts[1], parts[2])] = per.get((parts[1], parts[2]), 0) + v[0]
        by_owner["|".join(parts[:3])] = by_owner.get("|".join(parts[:3]), 0) + v[0]
    for (etype, dr), n in sorted(per.items()):
        if n > et[etype] * (1 + 1e-9):
            out.append(f"degree_by_far_label '{etype}|{dr}': its edges sum to {n:g}, above edge_totals['{etype}']")
    for k, n in sorted(by_owner.items()):   # over the far labels, an owner label's edges are its degree_hist row's sum d
        rows = (d.get("degree_hist") or {}).get(k)
        if isinstance(rows, list) and all(isinstance(r, list) and len(r) == 4 for r in rows):
            s1 = sum(r[2] for r in rows)
            if not _same(n, s1):
                out.append(f"degree_by_far_label '{k}|*': its edges sum to {n:g}, degree_hist '{k}' sums to {s1:g}")
    return [f"{x} (disagrees with tier 1; predict's load refuses it)" for x in out]


# ---- 2. against a dataset schema -------------------------------------------------------------------------------------

def check_schema(d, anchor, dschema):
    out = []
    labels = [v["label"] for v in dschema["vertices"]]
    etypes = {e["etype"]: e for e in dschema["edges"]}
    tc = d.get("type_counts", {})
    out += [f"type_counts.{l}: missing (the schema's label; 0 when the data has none)" for l in labels if l not in tc]
    for fld in ("edge_totals", "distinct_src", "distinct_dst"):
        m = d.get(fld, {})
        out += [f"{fld}.{e}: missing (the schema's edge type; 0 when the data has none)" for e in etypes if e not in m]
    content = d.get("sizes", {}).get("content", {})
    # every property of the schema has its content row (u = 0 when no vertex holds it): predict refuses a missing one
    # where a step reads it (DatasetStats::prop_content); this check finds it before predict
    for v in dschema["vertices"]:
        for p in v["props"]:
            if f"prop|{v['label']}|{p['name']}" not in content:
                out.append(f"sizes.content.prop|{v['label']}|{p['name']}: missing (the schema's property; u = 0 when no "
                           f"vertex holds it)")
    # every int property has its value_range entry (null when no element holds it): a range filter refuses a missing one
    vr = d.get("value_range") or {}
    ints = [f"prop|{v['label']}|{p['name']}" for v in dschema["vertices"] for p in v["props"] if p.get("type") == "int"]
    ints += [f"eprop|{e}|{p['name']}" for e, spec in etypes.items() for p in spec.get("props") or [] if p.get("type") == "int"]
    out += [f"value_range.{k}: missing (the schema's int property; null when no element holds it)" for k in ints if k not in vr]
    for e, spec in etypes.items():
        if not d.get("edge_totals", {}).get(e):
            continue                      # no edges: no end label to record
        for side, i in (("esrc", 0), ("edst", 1)):
            if not any(k.startswith(f"{side}|{e}|") for k in content):
                ends = sorted({p[i] for p in spec["endpoints"]})
                out.append(f"sizes.content.{side}|{e}|<label>: missing (the labels at that end: {', '.join(ends)})")
    if anchor is not None:
        for f in ("schema_hash", "records"):
            if anchor.get(f) != d.get(f):
                out.append(f"{f}: the anchor statistics were built from another {'schema' if f == 'schema_hash' else 'record stream'} "
                           f"than stats.json")
    return out


# ---- 3. what the plans read ------------------------------------------------------------------------------------------

DATE_OPS = {"read_index_range", "read_top_k_by_index"}
HOP_OPS = {op for op in KV_READ_OPS if PART[op] == "adjacency"}   # the reads that hop an adjacency (the op table's part)


def _str(x):
    return x if isinstance(x, str) else None


KEEP_OPS = {"dedup": "items", "exclude": "items", "pluck": "rows", "rename": "rows", "filter": "rows",
            "filter_in": "rows", "top_k": "rows"}   # the ops that keep a set's rows (cardinality.cc's producer_hop)


def producer_hop(ref, by_name):
    """the read_neighbors step a set came from, back through the ops that keep its rows (None when none)"""
    for _ in range(16):
        if not (isinstance(ref, dict) and "ref" in ref) or ref["ref"] not in by_name:
            return None
        s = by_name[ref["ref"]]
        if s.get("op") == "read_neighbors":
            return s
        arg = KEEP_OPS.get(s.get("op"))
        if arg is None or (s.get("op") == "pluck" and s.get("field") == "src") or arg not in s:
            return None
        ref = s[arg]
    return None


def needs_of(step, hints=None, by_name=None):
    """[(field, key or None, required?, the rule)]: what predict mode reads for one step of a plan (`hints`: the plan
    file's estimate_hints; `by_name`: the body's steps by name, to find a ranked walk's producer hop)"""
    op = step.get("op", "")
    out = []
    if op == "read_index_eq":
        label, prop = _str(step.get("label")), _str(step.get("prop"))
        if label and prop:
            out.append(("ndv", f"{label}|{prop}", True, "read_index_eq: matches = vertices / distinct values"))
            out.append(("top_values", f"{label}|{prop}", True, "read_index_eq: the value's vertices (its count, or the rest's spread)"))
    if op in DATE_OPS:
        labels = [l for l in step.get("labels") or [] if _str(l)] if isinstance(step.get("labels"), list) else []
        prop = _str(step.get("prop"))
        if labels and prop:   # the step's own dated vertices: their per-day counts (span, populated days, recency curve)
            for l in labels:
                out.append(("date_days", f"{l}|{prop}", True, f"{op}: the dates of the labels the step names"))
        else:
            out.append(("date_axis", None, True, f"{op} naming no labels or prop: the declared date axis"))
    if op == "read_top_k_by_index":
        out.append(("recency", None, False, "read_top_k_by_index from the anchor's neighbours: else the uniform walk"))
        hop = producer_hop(step.get("vids"), by_name or {})
        if hop is not None and _str(hop.get("etype")) and _str(step.get("prop")):
            src = (by_name or {}).get((hop.get("srcs") or {}).get("ref")) if isinstance(hop.get("srcs"), dict) else None
            k = step.get("k", 1)
            vids = step.get("vids")
            direct = isinstance(vids, dict) and vids.get("ref") == hop.get("name")   # the anchor hop itself, as C++ reads it
            own = (direct and src is not None and src.get("op") == "read_vertex_by_id" and "hi" not in step
                   and isinstance(k, (int, float)) and k <= 20)
            key = f"{hop['etype']}|{hop.get('dir') or 'out'}|{step['prop']}"
            # the anchor's own neighbours read their recency first; the burstiness only when it has no entry
            out.append(("burstiness", key, not own, "read_top_k_by_index over a hop's far ends: the walk's crossing"
                        + (" (when the recency has no entry)" if own else "")))
    et = _str(step.get("etype"))
    if op in HOP_OPS and et:
        for f in ("edge_totals", "distinct_src", "distinct_dst"):
            out.append((f, et, True, f"{op}: the hop's mean degree"))
        src = (by_name or {}).get((step.get("srcs") or {}).get("ref")) if isinstance(step.get("srcs"), dict) else None
        if (op == "read_neighbors" and src and src.get("op") == "read_index_eq" and "value" in src
                and step.get("dir", DEFAULT_DIR) in ("out", "in") and _str(src.get("label")) and _str(src.get("prop"))):
            out.append(("top_degrees", f"{src['label']}|{et}|{step.get('dir', DEFAULT_DIR)}|{src['prop']}", True,
                        "read_neighbors straight from an equality index read: the value's own edges"))
        if op in ("read_neighbors", "read_closure") and et in ((hints or {}).get("size_biased") or []):
            dr = step.get("dir") if step.get("dir") in ("out", "in", "both") else DEFAULT_DIR
            out.append(("degree_hist or neighbor_degree_ratio", f"{et}|{dr}", True,
                        f"{op} over {et}, declared size-biased: a neighbour's degree"))
    if op in ("read_chase", "read_closure"):
        out.append(("forest", et, False, f"{op}: the forest's mean depth; else a fixed depth or size"))
    if op in KV_READ_OPS:
        out.append(("owners", None, True, f"{op}: every KV step's owner groups"))
    return out


def plan_bodies(path):
    """[(body, steps)] of a plan file, and its estimate_hints"""
    with open(path) as f:
        ir = json.load(f)
    bodies = [(b, x.get("steps", [])) for b, x in ir["plans"].items()] if "plans" in ir else [("", ir.get("steps", []))]
    return bodies, ir.get("estimate_hints") or {}


def has_field(d, field, key):
    if field == "degree_hist or neighbor_degree_ratio":   # a neighbour's degree: the rows of E|dir, else the ratio
        return any(k.endswith("|" + key) for k in d.get("degree_hist") or {}) or key in (d.get("neighbor_degree_ratio") or {})
    if field not in d or d[field] is None:
        return False
    if key is None:
        return not (isinstance(d[field], (dict, list)) and not d[field])
    return key in d[field]


def check_plans(d, plan_dir):
    """(refusals, optional lines, plan files read, steps read)"""
    refused, optional = {}, {}
    files = sorted(glob.glob(os.path.join(plan_dir, "*.json")))
    n_steps = 0
    for p in files:
        bodies, hints = plan_bodies(p)
        for body, steps in bodies:
            by_name = {s.get("name"): s for s in steps}
            for s in steps:
                n_steps += 1
                for field, key, req, rule in needs_of(s, hints, by_name):
                    if has_field(d, field, key):
                        continue
                    name = field + (f".{key}" if key else "")
                    where = f"{os.path.basename(p)}{'@' + body if body else ''}"
                    (refused if req else optional).setdefault((name, rule), []).append(where)
    ref = [f"{name}: missing; read by {rule} -- plans {', '.join(sorted(set(w))[:4])}"
           f"{' and more' if len(set(w)) > 4 else ''}" for (name, rule), w in sorted(refused.items())]
    opt = [f"{name}: not given; {rule} ({len(set(w))} plan bodies)" for (name, rule), w in sorted(optional.items())]
    return ref, opt, len(files), n_steps


# ---- the tables ------------------------------------------------------------------------------------------------------

TIERS = [(1, "Tier 1: counts, one aggregate query each"), (2, "Tier 2: histograms and richer statistics"),
         ("anchor", "One vertex's own statistics (the anchor level only, never predict mode proper)"),
         ("file", "The file's identity")]


def render_tables(contract):
    req = set(contract["required"])
    lines = []
    for tier, title in TIERS:
        rows = [(k, v) for k, v in contract["properties"].items() if v.get("x-tier") == tier]
        if not rows:
            continue
        lines += [f"**{title}.**", "", "| field | what it is | per | needed for | without it | recipe | written by |",
                  "|---|---|---|---|---|---|---|"]
        for k, v in rows:
            need = v["x-need"] + ("; **load refuses a file without it**" if k in req else "")
            recipe = v["x-recipe"] if v["x-recipe"] in ("-", "written by the tool") else f"`{v['x-recipe']}`"
            cells = [f"`{k}`", v["description"], v["x-per"], need, v["x-missing"], recipe, v["x-written-by"]]
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def check_doc(path, contract):
    with open(path) as f:
        text = f.read()
    if BEGIN not in text or END not in text:
        return [f"{path}: no rendered block ({BEGIN} ... {END})"]
    block = text.split(BEGIN, 1)[1].split(END, 1)[0].strip("\n") + "\n"
    return [] if block == render_tables(contract) else [f"{path}: the rendered tables differ from the schema's "
                                                        f"(python -m kvevaluator.predict.check_stats --render-tables)"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("stats", nargs="?", help="stats.json (or one file holding every field)")
    ap.add_argument("--anchor-stats", help="the anchor tier read with it (graph-stats --anchor-out)")
    ap.add_argument("--schema", help="the compiled dataset schema (schema.json)")
    ap.add_argument("--plans", action="append", default=[], help="a dir of plan files (repeatable)")
    ap.add_argument("--contract", default=str(CONTRACT))
    ap.add_argument("--render-tables", action="store_true")
    ap.add_argument("--check-doc", metavar="PATH")
    a = ap.parse_args(argv)
    contract = load_contract(a.contract)
    if a.render_tables:
        sys.stdout.write(render_tables(contract))
        return 0
    if a.check_doc:
        bad = check_doc(a.check_doc, contract)
        print("\n".join(bad) if bad else f"check_stats: {a.check_doc}: the tables equal the schema's "
                                           f"({len(contract['properties'])} fields)")
        return 1 if bad else 0
    if not a.stats:
        ap.error("a statistics file, or --render-tables / --check-doc")
    d, prov, anchor = merged(a.stats, a.anchor_stats)
    refused = check_contract(d, prov, contract)
    print(f"check_stats: {a.stats}{' + ' + a.anchor_stats if a.anchor_stats else ''}: {len(d)} fields, the contract "
          f"({len(contract['properties'])} fields, {len(contract['required'])} required): {len(refused)} refused")
    r = check_tier1(d)
    print(f"check_stats: tier 2 against tier 1 ({len(d.get('degree_hist') or {})} degree rows, "
          f"{len(d.get('date_days') or {})} date rows, {len(d.get('neighbor_degree_ratio') or {})} ratios): {len(r)} refused")
    refused += r
    if a.schema:
        with open(a.schema) as f:
            ds = json.load(f)
        r = check_schema(d, anchor, ds)
        print(f"check_stats: against {a.schema}: {len(ds['vertices'])} labels, {len(ds['edges'])} edge types: {len(r)} refused")
        refused += r
    for pd in a.plans:
        r, opt, nf, ns = check_plans(d, pd)
        print(f"check_stats: the plans in {pd}: {nf} files, {ns} steps: {len(r)} refused, {len(opt)} optional fields not given")
        refused += r
        for o in opt:
            print(f"   optional: {o}")
        if nf == 0:
            refused.append(f"{pd}: no plan file read")
    for r in refused:
        print(f"   REFUSED {r}")
    print(f"check_stats: {'PASS' if not refused else 'FAIL'} ({len(refused)} refused)")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
