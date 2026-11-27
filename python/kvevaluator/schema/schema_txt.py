"""Parse a .schema text file into schemadef objects: (vertices, edges, indexes, groups, adj).

Line-oriented; '#' starts a comment. A keyword at column 0 starts a declaration; INDENTED lines belong to the
vertex or edge above. Each file is a complete schema; the parser holds no dataset facts.

  vertex <Label> prefix=<p> [group=<G>]
      <prop> <str|int|date|bytes> [multi] [nullable]   # a property (multi = list-valued; nullable = may be absent)
  edge <etype> [reverse]
      <Src>-><Dst> [<Src>-><Dst> ...]                  # endpoint pair(s): any line with '->'
      <prop> <str|int|date|bytes> [nullable]           # an edge property
  index <Label>                                        # a per-type membership index
  index <Label>.<prop> [type_scoped] [value_sorted|key_sorted] [equality]
                                                       # a property index; type_scoped keys it per type,
                                                       #   value_sorted / key_sorted imply type_scoped,
                                                       #   equality = an exact-value index on a number
  predicates per_label                                 # a predicate-named store (Dgraph) names a vertex
                                                       #   property `Label.prop`; needs the caller's `options`
  include <name>                                       # merge another schema's declarations first
  group <name> <Label>.<prop> <Label>.<prop> ...       # co-locate scalar columns in one key (a retired Dgraph
                                                       #   variant; NOT the vertex `group=` option)
  adj_sort <etype> <NeighborType>.<prop> [in|out]      # the declared side's adjacency, sorted by the neighbor's prop
  adj_inline <etype> <NeighborType>.<prop> [in|out]    # + that prop stored inline on the declared side
  adj_sort_facet <etype>.<facet> [in|out]              # the adjacency sorted by the edge's own facet
"""
import os

from kvevaluator.schema.schemadef import prop, vertex, edge, index
from kvevaluator.schema.schemadef import group as col_group          # aliased: `group` is a local var below (vertex group=)


def parse_schema(path, _seen=None, resolve=None, options=None):
    """(vertices, edges, indexes, groups, adj) parsed from `path`, ready for compile_schema.
    `adj` is [(mode, etype, prop, owner, dir)], mode 'sort'|'inline'|'sort_facet', dir 'in'|'out' (default 'in').
    `resolve(name) -> path` maps an `include` name to its file (default: `<name>.schema` beside `path`).
    Raises ValueError('<file>:<line>: <msg>') on any malformed line.
    """
    _seen = _seen if _seen is not None else set()
    rp = os.path.realpath(path)
    if rp in _seen:
        raise ValueError(f"{os.path.basename(path)}: include cycle")
    _seen.add(rp)
    vertices, edges, indexes, groups, adj = [], [], [], [], []
    name = os.path.basename(path)
    cur = None                                # the open vertex/edge block (a dict), or None

    def fail(lineno, msg):
        raise ValueError(f"{name}:{lineno}: {msg}")

    def close():                              # finalize the open block into an object
        nonlocal cur
        if cur is None:
            return
        if cur["kind"] == "vertex":
            vertices.append(vertex(cur["label"], cur["prefix"], cur["props"], group=cur["group"]))
        else:
            edges.append(edge(cur["etype"], reverse=cur["reverse"],
                              between=cur["pairs"] or None, props=cur["props"] or None))
        cur = None

    with open(path) as f:
        for lineno, raw in enumerate(f, 1):
            body = raw.split("#", 1)[0].rstrip()          # drop trailing comment + whitespace
            if not body.strip():
                continue
            indented = body[0] in " \t"
            toks = body.split()

            if not indented:
                close()
                head, rest = toks[0], toks[1:]
                if head == "vertex":
                    if not rest:
                        fail(lineno, "vertex needs a label")
                    label, prefix, group = rest[0], None, None
                    for opt in rest[1:]:
                        k, sep, v = opt.partition("=")
                        if not sep or not v:                  # reject `prefix` and empty `group=`
                            fail(lineno, f"expected key=value, got {opt!r}")
                        if k == "prefix":
                            prefix = v
                        elif k == "group":
                            group = v
                        else:
                            fail(lineno, f"unknown vertex option {k!r}")
                    if not prefix:
                        fail(lineno, f"vertex {label} needs prefix=<p>")
                    cur = {"kind": "vertex", "label": label, "prefix": prefix,
                           "group": group, "props": []}
                elif head == "edge":
                    if not rest:
                        fail(lineno, "edge needs an etype")
                    etype, reverse = rest[0], False
                    for opt in rest[1:]:
                        if opt in ("reverse", "reverse=true"):
                            reverse = True
                        else:
                            fail(lineno, f"unknown edge option {opt!r}")
                    cur = {"kind": "edge", "etype": etype, "reverse": reverse,
                           "pairs": [], "props": []}
                elif head == "index":
                    # `index Label` | `index Label.prop` | `index Label.prop <flag> [<flag>]`
                    # where each trailing flag is `type_scoped`, `value_sorted` or `key_sorted`
                    # (order-free, composable: `index Post.creationDate type_scoped key_sorted`).
                    _IX_FLAGS = ("type_scoped", "value_sorted", "key_sorted", "equality")
                    if len(toks) < 2:
                        fail(lineno, "index takes `Label`, `Label.prop`, or "
                                     "`Label.prop [type_scoped] [value_sorted|key_sorted]`")
                    target, flags = toks[1], toks[2:]
                    bad = [f for f in flags if f not in _IX_FLAGS]
                    if bad or len(set(flags)) != len(flags):
                        fail(lineno, f"index flags must be type_scoped/value_sorted/key_sorted/equality "
                                     f"(no dupes); got {flags}")
                    ts = "type_scoped" in flags               # trailing `type_scoped` flag
                    vs = "value_sorted" in flags              # trailing `value_sorted` flag
                    ks = "key_sorted" in flags                # trailing `key_sorted` flag
                    eq = "equality" in flags                  # trailing `equality` flag: an exact-value index on a number
                    try:
                        if "." in target:
                            lbl, p = target.split(".", 1)
                            indexes.append(index(lbl, p, kind="value" if eq else None, type_scoped=ts,
                                                 value_sorted=vs, key_sorted=ks))  # kind inferred unless `equality`
                        elif flags:
                            fail(lineno, "type_scoped/value_sorted/key_sorted need a property "
                                         "index target `Label.prop`")
                        else:
                            indexes.append(index(target))         # per-type membership index
                    except ValueError as e:
                        fail(lineno, str(e))
                elif head == "predicates":                    # predicates per_label
                    if rest != ["per_label"]:
                        fail(lineno, "the only form is `predicates per_label`")
                    if options is None:
                        fail(lineno, "`predicates per_label`: this schema's loader passes no `options` to parse_schema")
                    options["predicates"] = "per_label"
                elif head == "group":                         # group <name> Label.prop Label.prop ...
                    if len(toks) < 4:                         # keyword + name + >= 2 columns
                        fail(lineno, "group needs a name and >= 2 columns: "
                                     "`group <name> Label.prop Label.prop ...`")
                    gname, cols = toks[1], []
                    for t in toks[2:]:
                        if "." not in t:
                            fail(lineno, f"group column must be `Label.prop`, got {t!r}")
                        lbl, p = t.split(".", 1)
                        cols.append((lbl, p))
                    groups.append(col_group(gname, cols))
                elif head in ("adj_sort", "adj_inline"):      # adj_* <etype> <NbrType>.<prop> [in|out]
                    # The prop is owned by the NEIGHBOR VERTEX, so it is dotted with that
                    # neighbor's type. The optional trailing token DECLARES the walked direction
                    # (like an index's direction: the walked side carries the sort order and any
                    # inline copy); omitted = `in`, the reverse walk every recency query uses.
                    if len(toks) not in (3, 4) or "." not in toks[2] or (
                            len(toks) == 4 and toks[3] not in ("in", "out")):
                        fail(lineno, f"{head} takes `<etype> <NeighborType>.<prop> [in|out]` (the "
                                     f"neighbor vertex that owns the prop, e.g. `{head} hasCreator "
                                     f"Message.creationDate`; direction defaults to `in`)")
                    owner, pr = toks[2].split(".", 1)
                    d = toks[3] if len(toks) == 4 else "in"
                    adj.append((head[len("adj_"):], toks[1], pr, owner, d))  # (mode, etype, prop, owner, dir)
                elif head == "adj_sort_facet":                # adj_sort_facet <etype>.<facet> [in|out]
                    # DOTTED so the line is unambiguous on its own: the facet is the EDGE's own prop
                    # (likes.creationDate = the like's timestamp), never a vertex prop of that name.
                    if len(toks) not in (2, 3) or "." not in toks[1] or (
                            len(toks) == 3 and toks[2] not in ("in", "out")):
                        fail(lineno, "adj_sort_facet takes `<etype>.<facet> [in|out]` (the edge's own "
                                     "facet, e.g. `adj_sort_facet likes.creationDate`; direction "
                                     "defaults to `in`)")
                    et, fc = toks[1].split(".", 1)
                    d = toks[2] if len(toks) == 3 else "in"
                    adj.append(("sort_facet", et, fc, None, d))   # owner is the etype itself (None)
                elif head == "include":                       # include <name> -- merge a base schema first
                    if len(toks) != 2:
                        fail(lineno, "include takes one schema name (e.g. `include label_only`)")
                    incpath = (resolve(toks[1]) if resolve is not None
                               else os.path.join(os.path.dirname(path), f"{toks[1]}.schema"))
                    if not incpath or not os.path.exists(incpath):
                        fail(lineno, f"include {toks[1]!r}: no such schema ({incpath})")
                    iv, ie, ii, ig, ia = parse_schema(incpath, _seen, resolve, options)
                    vertices += iv; edges += ie; indexes += ii; groups += ig; adj += ia
                else:
                    fail(lineno, f"expected vertex/edge/index/group/adj_sort/adj_inline/"
                                 f"adj_sort_facet/include, got {head!r}")
                continue

            # indented -> a member of the current block
            if cur is None:
                fail(lineno, "indented line with no vertex/edge above it")
            if cur["kind"] == "vertex":                       # <name> <type> [multi] [nullable]
                if len(toks) < 2:
                    fail(lineno, f"expected `<name> <str|int|date> [multi] [nullable]`, got "
                                 f"{body.strip()!r}")
                flags = toks[2:]
                bad = [f for f in flags if f not in ("multi", "nullable")]
                if bad:
                    fail(lineno, f"unknown prop flag(s) {bad} (expected multi/nullable)")
                try:
                    cur["props"].append(prop(toks[0], toks[1], multi="multi" in flags,
                                             nullable="nullable" in flags))
                except ValueError as e:
                    fail(lineno, str(e))
            elif "->" in body:                                # edge endpoint pair(s)
                for pair in toks:
                    s, sep, d = pair.partition("->")
                    if not (sep and s and d) or "->" in d:    # exactly one arrow, both sides present
                        fail(lineno, f"expected `Src->Dst` pair(s), got {pair!r}")
                    cur["pairs"].append((s, d))
            else:                                             # edge property: <name> <type> [nullable]
                if not (2 <= len(toks) <= 3) or (len(toks) == 3 and toks[2] != "nullable"):
                    fail(lineno, f"expected `<name> <str|int|date> [nullable]` edge prop, got "
                                 f"{body.strip()!r}")
                try:
                    cur["props"].append(prop(toks[0], toks[1], nullable=(len(toks) == 3)))
                except ValueError as e:
                    fail(lineno, str(e))
    close()
    return vertices, edges, indexes, groups, adj
