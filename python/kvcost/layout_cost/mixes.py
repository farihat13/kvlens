"""A mixed layout's ASSIGNMENT ({unit: setting}, compose.unit_of's units): its text form ('knows lists=chunked; Message
props=condensed'), the meta spec that builds it (spec_of), and the assignment a spec builds (assignment). One parser,
one writer and one mapping each way, for the spec generator (pick_specs.py), the pricers and the checks.

spec_of: the components come from the grid's own cell specs (the inverse of grid_settings); the base cell is the
(props, adjacency) pair the assignment names most; every label of a vid group takes the group's layout (meta's rule);
Chunked at another chunk size ("chunked_c4k": compose.Composer.sizes) is
the grid's Chunked component with that `pack`; a per-direction unit ("adj", etype, "out"|"in") goes to
`adjacency_by_etype_dir` when the edge type's two directions differ (a direction the assignment does not name keeps
the base adjacency), else to `adjacency_by_etype`."""
import copy
import json
import os
import re

from kvevaluator.measure.grid_capture import GRID
from kvevaluator.layouts import cell_id
from kvevaluator.schema.vids import group_of
from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA
from kvcost.layout_cost import settings

LAYOUTS = settings.SETTINGS


# -- the text form ---------------------------------------------------------------------------------------------------

def unit_name(u):
    """a layout unit as the figures and CSVs spell it: 'Person props', 'knows lists', 'knows out-lists' ('index': None)"""
    if u is None:
        return "index"
    if u[0] == "props":
        return f"{u[1]} props"
    return f"{u[1]} {u[2]}-lists" if len(u) > 2 else f"{u[1]} lists"


def unit_of_name(name):
    """unit_name's inverse: 'Person props' | 'knows lists' | 'knows out-lists' -> the unit; SystemExit on another form"""
    if name.endswith(" props"):
        return ("props", name[:-len(" props")])
    for d in ("in", "out"):
        if name.endswith(f" {d}-lists"):
            return ("adj", name[:-len(f" {d}-lists")], d)
    if name.endswith(" lists"):
        return ("adj", name[:-len(" lists")])
    raise SystemExit(f"cannot parse unit {name!r} (want '<label> props', '<etype> lists' or '<etype> in-lists|out-lists')")


def assignment_label(assign):
    """'hasTag lists=split; knows lists=chunked; Message props=condensed' -- units in a stable order"""
    return "; ".join(f"{unit_name(u)}={l}" for u, l in sorted(assign.items(), key=lambda kv: (kv[0][0], kv[0][1:])))


def setting_short(setting):
    """a setting as a mix's text writes it, its size written: Sp, Co, Ch64k (the grid's Chunked), Ch4k (chunked_c4k)"""
    from kvevaluator import layouts
    m = re.fullmatch(r"(split|condensed|chunked)(?:_c(\d+)k)?", setting)
    if not m or (m.group(2) and m.group(1) != "chunked"):
        raise SystemExit(f"{setting!r} is not a setting (split, condensed, chunked or chunked_c<K>k)")
    return layouts.side_name(m.group(1), int(m.group(2)) if m.group(2) else None)


def setting_of(text):
    """setting_short's inverse: Sp | Co | Ch<K>k -> the setting id (split, condensed, chunked for 64 KB,
    chunked_c<K>k); the ids themselves are taken as they are"""
    from kvevaluator import layouts
    t = text.strip()
    if t in ("Sp", "Co"):
        return layouts.SHORT[t]
    m = re.fullmatch(r"Ch(\d+)k", t)
    if m:
        return layouts.setting_id("chunked", int(m.group(1)))
    if re.fullmatch(r"(split|condensed|chunked)(_c\d+k)?", t):
        return t
    raise SystemExit(f"{text!r} is not a setting: Sp, Co, Ch<K>k (a Chunked part carries its chunk size: Ch4k, "
                     f"Ch{layouts.GRID_CHUNK_KB}k)")


def assignment_text(assign):
    """an assignment as --mix takes it, the settings in short names with their sizes: 'knows lists=Ch4k; Message
    props=Co' -- units in a stable order"""
    return "; ".join(f"{unit_name(u)}={setting_short(l)}"
                     for u, l in sorted(assign.items(), key=lambda kv: (kv[0][0], kv[0][1:])))


def parse_assignment(text):
    """assignment_text's inverse: 'knows lists=Ch4k; Forum props=Co' -> {unit: setting id}; a setting id (chunked_c4k)
    is taken too"""
    out = {}
    for item in (x.strip() for x in text.split(";") if x.strip()):
        if "=" not in item:
            raise SystemExit(f"cannot parse {item!r} in {text!r} (want '<part>=<setting>; ...', e.g. 'knows lists=Ch4k')")
        name, setting = item.split("=", 1)
        out[unit_of_name(name.strip())] = setting_of(setting)
    return out


# -- assignment -> the meta spec that builds it ----------------------------------------------------------------------

def group_labels(group):
    """every schema label in a vid group (Message -> Post, Comment; any other label is its own group)"""
    return [lab for lab in LDBC_SCHEMA.vertex_props if group_of(lab) == group] or [group]


def components():
    """{"props"|"adj": {layout name: component json}} from the grid's cells"""
    out = {"props": {}, "adj": {}}
    for p in LAYOUTS:
        for a in LAYOUTS:
            spec = json.load(open(os.path.join(GRID, cell_id(p, a) + ".json")))
            out["props"][p], out["adj"][a] = spec["vertex_props"], spec["adjacency"]
    return out


def sized_pack(layout):
    """the chunk bytes of a size option ('chunked_c4k' -> 4096), None for any other layout"""
    m = re.fullmatch(r"chunked_c(\d+)k", layout)
    return int(m.group(1)) * 1024 if m else None


def spec_of(assign, comps=None):
    """(spec, base cell name) of a per-type or per-type-and-direction assignment; Chunked at another chunk size is
    written as Chunked with its own pack"""
    comps = copy.deepcopy(comps or components())
    for l in sorted({l for l in assign.values() if sized_pack(l)}):
        for kind in ("props", "adj"):
            comps[kind][l] = copy.deepcopy(comps[kind]["chunked"])
            comps[kind][l]["owners"]["pack"] = sized_pack(l)
    def lay(l):
        if l in LAYOUTS or sized_pack(l):
            return l
        raise SystemExit(f"spec_of: {l!r} is not a layout (the grid's three, or chunked_c<N>k)")
    props = {u[1]: lay(l) for u, l in assign.items() if u[0] == "props"}
    adj = {u[1]: lay(l) for u, l in assign.items() if u[0] == "adj" and len(u) == 2}
    dirs = {}                                            # etype -> {"out"|"in": layout} of the per-direction units
    for u, l in assign.items():
        if u[0] == "adj" and len(u) == 3:
            dirs.setdefault(u[1], {})[u[2]] = lay(l)
    for e, d in list(dirs.items()):                      # both directions alike (or one named): the etype's layout
        if len(set(d.values())) == 1:
            adj[e] = next(iter(d.values()))
            del dirs[e]
    every = list(adj.values()) + [l for d in dirs.values() for l in d.values()]
    base_p = max(LAYOUTS, key=lambda l: (sum(v == l for v in props.values()), -LAYOUTS.index(l)))
    base_a = max(LAYOUTS, key=lambda l: (sum(v == l for v in every), -LAYOUTS.index(l)))
    spec = json.load(open(os.path.join(GRID, cell_id(base_p, base_a) + ".json")))
    by_label = {lab: comps["props"][l] for group, l in sorted(props.items()) if l != base_p for lab in group_labels(group)}
    by_etype = {e: comps["adj"][l] for e, l in sorted(adj.items()) if l != base_a}
    by_dir = {e: {d: comps["adj"][l] for d, l in sorted(dl.items())} for e, dl in sorted(dirs.items())}
    if by_label:
        spec["vertex_props_by_label"] = by_label
    if by_etype:
        spec["adjacency_by_etype"] = by_etype
    if by_dir:
        spec["adjacency_by_etype_dir"] = by_dir
    return spec, cell_id(base_p, base_a)


# -- a spec -> the assignment it builds ------------------------------------------------------------------------------

def grid_settings(sizes=()):
    """{"props"|"adj": {canonical component json: layout name}} from the grid's own cells, and Chunked at each other
    chunk size in `sizes` (option ids, chunked_c4k: Composer.sizes) as spec_of writes it: the grid's Chunked component
    with that size's pack"""
    out = {"props": {}, "adj": {}}
    for p in LAYOUTS:
        for a in LAYOUTS:
            spec = json.load(open(os.path.join(GRID, cell_id(p, a) + ".json")))
            out["props"][json.dumps(spec["vertex_props"], sort_keys=True)] = p
            out["adj"][json.dumps(spec["adjacency"], sort_keys=True)] = a
    comps = components() if sizes else None
    for oid in sorted(set(sizes)):
        for kind in ("props", "adj"):
            c = copy.deepcopy(comps[kind]["chunked"])
            c["owners"]["pack"] = sized_pack(oid)
            out[kind][json.dumps(c, sort_keys=True)] = oid
    return out


def layout_name(settings, kind, component, where):
    key = json.dumps(component, sort_keys=True)
    if key not in settings[kind]:
        raise SystemExit(f"{where}: {component} matches no grid {kind} setting")
    return settings[kind][key]


# The built mixes whose props are split into rows (vertex_props.groups). No grouped props option
# exists, so they have no assignment to price; their captures stay, and the readers skip them by name.
RETIRED_GROUPED_MIXES = ("people_hot_cold_messages_w16_fewest_bytes",)


def retired_grouped(spec, name):
    """True for a built mix the readers skip: one whose props are split into rows (vertex_props.groups), an option
    retired; one not in RETIRED_GROUPED_MIXES is skipped too, warned and marked"""
    if not spec["vertex_props"].get("groups"):
        return False
    if name not in RETIRED_GROUPED_MIXES:
        from kvevaluator import stale
        stale.note("capture_shape_moved", f"{name}: its props are split into rows (vertex_props.groups), and it is not one "
                   f"of the grouped built mixes ({', '.join(RETIRED_GROUPED_MIXES)}): skipped, as they are (the "
                   f"search offers no grouped props options)")
    return True


def assignment(spec, units, settings, name, swap=None):
    """{unit: layout}: the grid settings the spec's components match. A spec that splits a label's props into rows
    (vertex_props.groups) is refused: the search offers no grouped props options. An adjacency unit is ("adj", etype) or, per direction,
    ("adj", etype, "out"|"in"): the direction's override (adjacency_by_etype_dir), else the etype's, else the default. swap = "out"|"in": that direction's units take the
    other direction's layout (the control)."""
    if spec["vertex_props"].get("groups"):
        raise SystemExit(f"{name}: its props are split into rows (vertex_props.groups): the search offers no "
                         f"grouped props options, so it has no assignment to price")
    dp = layout_name(settings, "props", spec["vertex_props"], f"{name} vertex_props")
    da = layout_name(settings, "adj", spec["adjacency"], f"{name} adjacency")
    by_label = {k: layout_name(settings, "props", v, f"{name} props of {k}")
                for k, v in spec.get("vertex_props_by_label", {}).items()}
    by_etype = {k: layout_name(settings, "adj", v, f"{name} adjacency of {k}")
                for k, v in spec.get("adjacency_by_etype", {}).items()}
    def props_of(group):   # a props unit is a vid group; meta requires its labels to agree
        got = {by_label.get(l, dp) for l in group_labels(group)}
        if len(got) > 1:
            raise SystemExit(f"{name}: the labels of vid group {group} have different props layouts")
        return got.pop()
    by_dir = {e: {d: layout_name(settings, "adj", c, f"{name} {d} lists of {e}") for d, c in dirs.items()}
              for e, dirs in spec.get("adjacency_by_etype_dir", {}).items()}
    def adj_of(u):
        if len(u) == 2:
            if u[1] in by_dir:
                raise SystemExit(f"{name}: {u[1]} has per-direction layouts; price it on per-direction units (by_dir)")
            return by_etype.get(u[1], da)
        d = u[2] if swap != u[2] else ("in" if u[2] == "out" else "out")
        return by_dir.get(u[1], {}).get(d, by_etype.get(u[1], da))
    return {u: (props_of(u[1]) if u[0] == "props" else adj_of(u)) for u in units}
