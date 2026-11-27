"""The layout grammar, the one parser of a layout's name: a short name ('Co-Ch64k', 'Co-Ch4k', 'Ch1k-Ch4k'), a cell id, a
mix's spec file or a named format -> the one spec file the configurable format `meta` is built from; and the spec short
form, a spec written in our words. Every command that takes layouts parses them here (resolve; arg_type for argparse).

A layout is properties-adjacency in short names (Sp Split, Co Condensed, Ch Chunked), the paper's: each Chunked side
carries its chunk size, any size in KB (Co-Ch4k, Ch1k-Ch4k, Ch2k-Ch2k; the grid's own size is Ch64k). A name's spec is
made from the name (spec_of: the settings differ only in the Chunked side's `pack`); the spec dirs of inputs/specs
(ID_DIRS) hold the measured ones as files, each equal to its generated spec (check_spec_table), and a name no dir holds
gets its generated spec in the output dir's specs/. A mix (a spec file) is named by its content, mix_<sha> (mix_name).

The short form (expand / short_form): each part of a spec is one word, Split, Condensed or Chunked@<size>, e.g.
    {"vertex_props": "Chunked@64k", "adjacency": "Chunked@64k", "adjacency_by_etype": {"knows": "Condensed"},
     "index": "Split"}
A part the words do not cover (an edge-property placement, a grouped or day index) is written in the full form, which is
always accepted. Neutral infra: the measurement side (the grid's spec sets, the chunk sizes) and the pricer read it.
"""
import glob
import json
import os
import re

from kvevaluator.paths import ROOT

EXP = str(ROOT)
SPECS = os.path.join(EXP, "inputs", "specs")   # the spec sets, one dir per set; a store and a capture are named by its file stems
GRID = os.path.join(SPECS, "grid")             # the 9 uniform cells: the default spec set

SETTINGS = ("split", "condensed", "chunked")     # the grid's three; a cell takes one per side
CODE = {"split": "Sp", "condensed": "Co", "chunked": "Ch"}
SHORT = {v: k for k, v in CODE.items()}         # 'Sp' -> 'split'
CELLS = [f"props_{p}_adj_{a}" for p in SETTINGS for a in SETTINGS]

# The chunk sizes of the Chunked setting the composed layouts read: the grid's own Chunked is 64 KB (inputs/specs/grid);
# another size K is its own spec set, inputs/specs/grid_chunked_<K>k.
GRID_CHUNK_KB = 64
CHUNK_SIZES_KB = (1, 4, 16, 64, 256)
# chunk size -> the spec dir that holds that size's cells: the dirs the published sized runs use
# (run-bi-cells.sh --chunked-size, kvcost measure Ch<size>-Ch<size>). Only 4k holds all five Chunked-side cells.
SIZE_DIRS = {"1k": "grid_chunked_1k", "4k": "grid_chunked_4k", "16k": "grid_chunked_16k", "128k": "grid_chunked_128k",
             "256k": "grid_chunked_256k"}
# dirs searched for a cell id given in full, in this order (a size per side: grid_sized; added stores: grid_added)
ID_DIRS = ("grid",) + tuple(SIZE_DIRS.values()) + ("grid_sized", "grid_added")
NAMED = ("dgraph", "nebula", "arangodb")      # the named formats (grid_capture.FORMATS); measured on JOB only

_ID = re.compile(r"(?:meta-)?props_(split|condensed|chunked)(?:_c(\d+)k)?_adj_(split|condensed|chunked)(?:_c(\d+)k)?")
# The id spellings of one size (OLD_FORM): a ChK-ChK id that names one size. props_chunked_adj_chunked_c4k meant Ch4k-Ch4k as a
# store or spec stem and Ch64k-Ch4k as a composed row's cell, so it is refused everywhere (scripts/rename_cell_ids.py
# renamed the data once). Searched, not matched: a stem may carry a suffix (props_..._c4k_own).
OLD_FORM = re.compile(r"props_chunked_adj_chunked_c\d+k|props_chunked_c\d+k_adj_chunked(?!_c\d)")
_RULE = ("a ChK-ChK id names both sizes or neither: props_chunked_c4k_adj_chunked_c4k (4 KB both sides), "
         "props_chunked_c64k_adj_chunked_c4k (64 KB properties), props_chunked_adj_chunked (64 KB both)")


def setting_id(layout, kb=None):
    """'chunked_c4k' of ('chunked', 4); 'chunked' of ('chunked', None or 64); 'split' of ('split', None)"""
    if layout not in SETTINGS:
        raise ValueError(f"not a setting: {layout}")
    if kb is not None and layout != "chunked":
        raise ValueError(f"{layout} takes no chunk size")
    return "chunked" if layout == "chunked" and kb in (None, GRID_CHUNK_KB) else (
        f"chunked_c{kb}k" if layout == "chunked" else layout)



# The diagonals: every setting a part can take, Split, Condensed and Chunked at each chunk size, in display order. A
# uniform layout is a pair of them; plan validity follows the kind (SETTINGS: any chunk size has Chunked's).
DIAGONALS = ("split", "condensed") + tuple(setting_id("chunked", kb) for kb in CHUNK_SIZES_KB)
# the chunk sizes besides the 3x3 cells' own 64 KB, by name ("1k", ...): each one's Chunked is costed from its own ChK-ChK
OTHER_CHUNK_SIZES = tuple(f"{kb}k" for kb in CHUNK_SIZES_KB if kb != GRID_CHUNK_KB)

def _setting_parts(setting):
    m = re.fullmatch(r"(split|condensed|chunked)(?:_c(\d+)k)?", setting)
    if not m or (m.group(2) and m.group(1) != "chunked"):
        raise ValueError(f"not a setting: {setting} (split, condensed, chunked or chunked_c<K>k)")
    return m.group(1), int(m.group(2)) if m.group(2) else None


def cell_id(props_setting, adj_setting):
    """THE rule for a uniform layout's id: props_<setting>_adj_<setting>, each side split, condensed, chunked (64 KB)
    or chunked_c<K>k; when both sides are Chunked and either is not 64 KB, both carry their size (64 KB as _c64k).
    Inside a composed size grid's dir (composed-c<K>k/), an id is relative to that grid's size."""
    p, pk = _setting_parts(props_setting)
    a, ak = _setting_parts(adj_setting)
    pk = None if pk == GRID_CHUNK_KB else pk
    ak = None if ak == GRID_CHUNK_KB else ak
    if p == a == "chunked" and (pk or ak):
        pk, ak = pk or GRID_CHUNK_KB, ak or GRID_CHUNK_KB
        return f"props_chunked_c{pk}k_adj_chunked_c{ak}k"
    return f"props_{setting_id(p, pk)}_adj_{setting_id(a, ak)}"


# The stores the paper's grid runs measure (kvcost measure ... Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k
# Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k): the three diagonals and ChK-ChK at every other chunk size. Every other layout composes from them (kvcost.layout_cost.compose_cells); a
# layout measured itself is named as a layout (Co-Ch64k), never as a set.
SEVEN = (("props_split_adj_split", "props_condensed_adj_condensed")
         + tuple(cell_id(f"chunked_c{k}k", f"chunked_c{k}k") for k in CHUNK_SIZES_KB))
GRID_MEASURED = tuple(c for c in SEVEN if c in CELLS)   # the grid set's cells a default run measures: the three diagonals


def cell_runs(text):
    """[(spec set dir name, [cells])] of a grid run's --cells: comma-separated cell ids, the grid's own set first, then
    each chunk-size set (SIZE_DIRS) in size order. A ChK-ChK id at another size goes to its size's set; any other id stays
    in the grid's set, where the measuring tools check it"""
    by_set = {}
    for c in [t for t in text.split(",") if t]:
        m = re.fullmatch(r"props_chunked_c(\d+)k_adj_chunked_c\1k", c)
        d = SIZE_DIRS.get(f"{m.group(1)}k", "grid") if m else "grid"
        if c not in by_set.setdefault(d, []):
            by_set[d].append(c)
    order = ["grid"] + [SIZE_DIRS[k] for k in sorted(SIZE_DIRS, key=lambda k: int(k[:-1]))]
    return [(d, by_set[d]) for d in order if d in by_set]


def check_no_old_form(text, what="id"):
    """ValueError when text holds an id in the one-size spelling (OLD_FORM)"""
    m = OLD_FORM.search(text)
    if m:
        raise ValueError(f"{what} {text!r}: {m.group(0)} is the old spelling, refused; {_RULE}")


def cell_parts(cell):
    """('chunked', 4, 'split', None) of props_chunked_c4k_adj_split: (properties layout, its chunk KB, adjacency layout,
    its chunk KB); a size is None when the id gives none (a bare Chunked side is 64 KB). ValueError when the id is not a
    cell, sizes a non-Chunked part, or is not written by cell_id (the old ChK-ChK spellings among them)"""
    m = _ID.fullmatch(cell)
    if not m:
        raise ValueError(f"not a cell: {cell}")
    p, pk, a, ak = m.group(1), m.group(2), m.group(3), m.group(4)
    for layout, k in ((p, pk), (a, ak)):
        if k is not None and layout != "chunked":
            raise ValueError(f"not a cell: {cell} ({layout} takes no chunk size)")
    check_no_old_form(cell, "cell")
    bare = cell[len("meta-"):] if cell.startswith("meta-") else cell
    canon = cell_id(f"{p}_c{pk}k" if pk else p, f"{a}_c{ak}k" if ak else a)
    if bare != canon:
        raise ValueError(f"cell {cell!r} is written {canon}; {_RULE}")
    return p, int(pk) if pk else None, a, int(ak) if ak else None


def is_cell(text):
    """True when text is a cell id (cell_parts reads it); False for another name (a mix); ValueError for an id cell_id
    does not write (the old spellings among them)"""
    if not _ID.fullmatch(text):
        check_no_old_form(text)
        return False
    cell_parts(text)
    return True


def spec_part(part):
    """(layout, KB) of one part of a meta spec: group item = Split; group owner, owners one = Condensed; owners
    {pack: N} = Chunked at N bytes"""
    owners = part.get("owners")
    if isinstance(owners, dict) and "pack" in owners:
        return "chunked", owners["pack"] // 1024
    return ("split" if part.get("group") == "item" else "condensed"), None


def cell_settings(cell):
    """('chunked_c4k', 'chunked'): the props and adjacency settings of a cell id, sizes kept and spelled as setting_id
    writes them (64 KB as 'chunked', also where the id writes _c64k); cell_id(*cell_settings(c)) == c"""
    p, pk, a, ak = cell_parts(cell)
    return setting_id(p, pk), setting_id(a, ak)


def settings_of(cell):
    """('condensed', 'chunked'): the props and adjacency settings of a cell id, sizes dropped"""
    p, _pk, a, _ak = cell_parts(cell)
    return p, a


class Layout:
    """one resolved layout: its cell id (the capture's file stem), its spec file and spec dir (None for the grid's
    own nine, as the drivers' default), and the name it is shown by"""

    def __init__(self, cell, spec, spec_dir, name, named=None):
        self.cell, self.spec, self.spec_dir, self.name, self.named = cell, spec, spec_dir, name, named

    @property
    def is_grid(self):
        return self.spec_dir is None and self.named is None

    @property
    def slug(self):
        """the view's directory name: the shown name ('Co-Ch4k'), a named format by its name"""
        return self.name

    def settings(self):
        """('condensed', 'chunked'): the props and adjacency settings, sizes dropped"""
        return settings_of(self.cell)

    def __repr__(self):
        return f"Layout({self.name}: {self.cell}, {os.path.relpath(self.spec, EXP) if self.spec else '-'})"


def side_name(layout, kb=None):
    """THE rule for a side's shown name: 'Sp', 'Co', and a Chunked side always with its chunk size, 'Ch4k'; the grid's
    64 KB (no size in a cell id) is 'Ch64k'"""
    return CODE[layout] + (f"{kb or GRID_CHUNK_KB}k" if layout == "chunked" else "")


def family_name(props, adj):
    """a layout with its chunk size left open, K: 'ChK-ChK', 'Co-ChK', 'Sp-Co' of two of SETTINGS; for a table whose
    rows are a layout at each chunk size in turn (never a layout's own name: name_of)"""
    return "-".join(CODE[s] + ("K" if s == "chunked" else "") for s in (props, adj))


def name_of(cell):
    """the shown name, the paper's: properties-adjacency, each side by side_name: 'Sp-Co', 'Co-Ch64k', 'Co-Ch4k',
    'Ch64k-Ch16k'; resolve reads it back"""
    p, pk, a, ak = cell_parts(cell)
    return f"{side_name(p, pk)}-{side_name(a, ak)}"


def cell_of_spec(spec):
    """the cell id (cell_id's rule) of a uniform spec (one vertex_props and one adjacency part), None for another spec
    (a mix)"""
    if not isinstance(spec, dict) or not {"vertex_props", "adjacency"} <= set(spec):
        return None
    try:
        (p, pk), (a, ak) = spec_part(spec["vertex_props"]), spec_part(spec["adjacency"])
    except (AttributeError, TypeError):
        return None
    return cell_id(setting_id(p, pk), setting_id(a, ak))


def _spec_dir_or_none(d):
    return None if d == "grid" else os.path.join(SPECS, d)


NAME = re.compile(r"(Sp|Co|Ch)(\d+k)?-(Sp|Co|Ch)(\d+k)?")      # a layout's short name: props-adjacency
NAME_RULE = ("props-adjacency in short names (Sp, Co, Ch<K>k: a Chunked side carries its chunk size in KB), "
             "e.g. Sp-Co, Co-Ch64k or Ch1k-Ch16k")


def cell_of_name(text):
    """the cell id of a short name ('Co-Ch4k' -> props_condensed_adj_chunked_c4k); SystemExit when it is not one"""
    m = NAME.fullmatch(text)
    if not m:
        raise SystemExit(f"{text!r} is not a layout name: {NAME_RULE}")
    sides = [(SHORT[m.group(1)], m.group(2)), (SHORT[m.group(3)], m.group(4))]
    if any(k and lay != "chunked" for lay, k in sides):
        raise SystemExit(f"{text}: a size goes on a Chunked side only (Ch4k), never on Split or Condensed")
    if any(k and int(k[:-1]) < 1 for _, k in sides):
        raise SystemExit(f"{text}: a chunk size is at least 1k")
    if any(lay == "chunked" and not k for lay, k in sides):
        full = "-".join(side_name(lay, int(k[:-1]) if k else None) for lay, k in sides)
        raise SystemExit(f"{text}: a Chunked side carries its chunk size; the grid's {GRID_CHUNK_KB} KB is written "
                         f"{full}")
    return cell_id(*(setting_id(lay, int(k[:-1]) if k else None) for lay, k in sides))


def cell_of(text):
    """the cell id a short name or a cell id names, with no spec read ('Co-Ch4k' -> props_condensed_adj_chunked_c4k);
    SystemExit for anything else"""
    t = text.strip()
    if NAME.fullmatch(t):
        return cell_of_name(t)
    try:
        if is_cell(t):
            return t
    except ValueError as e:
        raise SystemExit(str(e))
    raise SystemExit(f"{t!r} is not a uniform layout: write {NAME_RULE}, or a cell id")


def resolve(text):
    """'Co-Ch64k' | 'Co-Ch4k' | 'Ch1k-Ch4k' (the paper's names: each Chunked side with its size, the grid's as Ch64k)
    | 'props_condensed_adj_chunked_c4k' | 'dgraph' | a spec file -> Layout; SystemExit with the choices"""
    t = text.strip()
    if t in NAMED:
        return Layout(t, None, None, t, named=t)
    if t.endswith(".json"):
        return from_spec(t)
    if NAME.fullmatch(t):
        return _resolve_id(t, cell_of_name(t))
    if re.fullmatch(r"props_\w+_adj_\w+", t):
        try:
            is_cell(t)                                    # refuses an old spelling, with or without a suffix
        except ValueError as e:
            raise SystemExit(str(e))
        return _resolve_id(t, t)
    raise SystemExit(f"{t!r} is not a layout: write {NAME_RULE}, a cell id (props_condensed_adj_chunked), a spec file "
                     f"(my_layout.json), or a named format ({', '.join(NAMED)})")


def spec_of(cell):
    """the full-form spec of a uniform layout, made from its cell id: each side's setting as its word (Split, Condensed,
    Chunked@<K>k), the index Split; every cell-id file in ID_DIRS equals it (check_spec_table)"""
    p, pk, a, ak = cell_parts(cell)

    def word(lay, kb):
        return {"split": "Split", "condensed": "Condensed"}.get(lay) or f"Chunked@{kb or GRID_CHUNK_KB}k"
    return expand({"vertex_props": word(p, pk), "adjacency": word(a, ak), "index": "Split"})


def generated_dir():
    """where a name's generated spec goes when no spec dir holds it: the output tree's specs/"""
    from kvevaluator.paths import specs_dir
    return str(specs_dir())


def _resolve_id(t, cell):
    """the Layout of a cell id: its spec in the first of ID_DIRS that holds it (the grid, then each size's set), else its
    generated spec (spec_of), written to generated_dir() when that file is missing or differs"""
    for d in ID_DIRS:
        spec = os.path.join(SPECS, d, cell + ".json")
        if os.path.exists(spec):
            return Layout(cell, spec, _spec_dir_or_none(d), name_of(cell))
    d = generated_dir()
    spec = os.path.join(d, cell + ".json")
    text = json.dumps(spec_of(cell), indent=2) + "\n"
    if not (os.path.exists(spec) and open(spec).read() == text):
        os.makedirs(d, exist_ok=True)
        with open(spec, "w") as f:
            f.write(text)
    return Layout(cell, spec, d, name_of(cell))


def mix_name(spec):
    """a mix's name, made from its content: mix_<sha> of its full form (expand), so one name never holds two specs and
    two files of one stem never share a name (the store, the captures and the timings are shared)"""
    import hashlib
    return "mix_" + hashlib.sha256(json.dumps(expand(spec), sort_keys=True).encode()).hexdigest()[:10]


def _is_name_or_id(text):
    """True for a short name (its sizes checked: SystemExit on a bad one) or a cell id's spelling"""
    if NAME.fullmatch(text):
        cell_of_name(text)
        return True
    return bool(re.fullmatch(r"props_\w+_adj_\w+", text)
                and _ID.fullmatch(text[len("meta-"):] if text.startswith("meta-") else text))


def arg_type(*, files=True, dirs=False, formats=False, words=()):
    """an argparse type for a command's layouts, the grammar's syntax only (no spec read, no disk but a path's kind): a
    short name, a cell id, a spec file (files), a dir of them (dirs), a named format (formats: dgraph, nebula,
    arangodb, pertype_csr, each with a -c<K>k chunk arm), or one of `words`; anything else is refused with the rule"""
    import argparse

    kinds = [(True, _is_name_or_id), (files, lambda t: t.endswith(".json")), (dirs, os.path.isdir),
             (formats, lambda t: re.fullmatch(rf"({'|'.join(NAMED + ('pertype_csr',))})(-c\d+k)?", t)),
             (bool(words), lambda t: t in words)]

    def check(text):
        t = text.strip()
        if any(on and ok(t) for on, ok in kinds):
            return t
        what = [NAME_RULE, "a cell id (props_condensed_adj_chunked)"] + (["a mix's spec file (my_mix.json)"] if files else []) \
            + (["a dir of spec files"] if dirs else []) + ([f"a named format ({', '.join(NAMED)})"] if formats else []) \
            + ([f"one of {', '.join(words)}"] if words else [])
        raise argparse.ArgumentTypeError(f"{t!r} is not a layout: write " + "; or ".join(what))
    check.__name__ = "layout"
    return check


def grid_layouts():
    """the nine uniform cells, in CELLS order"""
    return [resolve(c) for c in CELLS]


def check_spec_table():
    """every cell id in ID_DIRS: one content per id (a duplicate stem must be byte-identical JSON), every spec named by
    cell_id of its own content (no spec file in inputs/specs holds an old spelling), and every short name with a size
    resolves to a file that exists. Returns (ids checked, duplicates); SystemExit on a conflict."""
    for p in sorted(glob.glob(os.path.join(SPECS, "*", "*.json"))):
        stem = os.path.basename(p)[:-5]
        if OLD_FORM.search(stem):
            raise SystemExit(f"{os.path.relpath(p, EXP)}: an old spelling; {_RULE}")
        m = _ID.match(stem)
        canon = cell_of_spec(json.load(open(p))) if m else None
        if canon and m.group(0) != canon:
            raise SystemExit(f"{os.path.relpath(p, EXP)}: its content is {canon}, its name says {m.group(0)}")
        if _ID.fullmatch(stem) and json.load(open(p)) != spec_of(stem):
            raise SystemExit(f"{os.path.relpath(p, EXP)}: differs from the spec made from its name (spec_of)")
    seen, dups = {}, []
    for d in ID_DIRS:
        for p in sorted(glob.glob(os.path.join(SPECS, d, "*.json"))):
            stem = os.path.basename(p)[:-5]
            body = json.load(open(p))
            if stem in seen:
                if seen[stem][1] != body:
                    raise SystemExit(f"{stem}: {seen[stem][0]} and {d} hold different specs under one name")
                dups.append((stem, seen[stem][0], d))
            else:
                seen[stem] = (d, body)
    for size in SIZE_DIRS:
        resolve(f"Ch{size}-Ch{size}")                     # the same size on both sides exists at every listed size
    return len(seen), dups


def from_spec(path):
    """a mix (or any spec file) to build: its stem is the cell id, its dir the driver's --spec-dir"""
    p = os.path.abspath(path)
    if not (p.endswith(".json") and os.path.exists(p)):
        raise SystemExit(f"{path}: not a spec json")
    if OLD_FORM.search(os.path.basename(p)):
        raise SystemExit(f"{path}: an old spelling; {_RULE}")
    return Layout(os.path.basename(p)[:-5], p, os.path.dirname(p), os.path.basename(p)[:-5])


# --- the spec short form ------------------------------------------------------------------------------------------
# One word per part. Condensed adjacency overflows one owner's list into parts at 512 KB (the field `split`, where one
# value is cut, not the Split setting); Condensed properties never overflow. The index's word is Split only: one item
# per entry, exact terms, scoped per type.
CONDENSED_ADJ_SPLIT = 524288
_PART_KINDS = {"vertex_props": "props", "vertex_props_by_label": "props", "adjacency": "adj",
               "adjacency_by_etype": "adj", "adjacency_by_etype_dir": "adj", "index": "index"}


def size_bytes(text):
    """'64k' -> 65536"""
    m = re.fullmatch(r"(\d+)k", text)
    if not m:
        raise SystemExit(f"{text!r}: a chunk size is <N>k, e.g. 4k or 64k")
    return int(m.group(1)) * 1024


def _word_part(word, kind):
    """one part's full form from its word"""
    if kind == "index":
        if word != "Split":
            raise SystemExit(f"index {word!r}: the index's short word is Split (write another index in the full form)")
        return {"group": "item", "owners": "one", "split": "never", "term": "exact", "scope": "type"}
    if word == "Split":
        return {"group": "item", "owners": "one", "split": "never"}
    if word == "Condensed":
        return {"group": "owner", "owners": "one", "split": CONDENSED_ADJ_SPLIT if kind == "adj" else "never"}
    m = re.fullmatch(r"Chunked@(\d+k)", word)
    if m:
        return {"group": "owner", "owners": {"pack": size_bytes(m.group(1))}, "split": "never"}
    raise SystemExit(f"{word!r}: a part is Split, Condensed or Chunked@<size> (e.g. Chunked@64k), or a full-form object")


def _part_word(part, kind):
    """one part's word, or None when no word expands to it exactly"""
    if not isinstance(part, dict):
        return None
    words = ["Split"] if kind == "index" else ["Split", "Condensed"]
    if kind != "index" and isinstance(part.get("owners"), dict) and "pack" in part["owners"] \
            and part["owners"]["pack"] % 1024 == 0:
        words.append(f"Chunked@{part['owners']['pack'] // 1024}k")
    for w in words:
        if _word_part(w, kind) == part:
            return w
    return None


def expand(spec):
    """a spec in the short form (or the full form, or a mix of both) -> the full form `meta` reads"""
    out = {"version": 1, "direction": "declared"}
    for key, value in spec.items():
        kind = _PART_KINDS.get(key)
        if kind is None:
            out[key] = value
        elif key in ("vertex_props", "adjacency", "index"):
            out[key] = _word_part(value, kind) if isinstance(value, str) else value
        elif key == "adjacency_by_etype_dir":
            out[key] = {e: {d: (_word_part(v, kind) if isinstance(v, str) else v) for d, v in dirs.items()}
                        for e, dirs in value.items()}
        else:
            out[key] = {n: (_word_part(v, kind) if isinstance(v, str) else v) for n, v in value.items()}
    return out


def short_form(spec):
    """a full-form spec -> the same spec in words wherever a word expands to the part exactly (expand inverts it)"""
    def word(v, kind):
        return _part_word(v, kind) or v
    out = {}
    for key, value in spec.items():
        kind = _PART_KINDS.get(key)
        if key in ("version", "direction") and value == {"version": 1, "direction": "declared"}[key]:
            continue
        if kind is None:
            out[key] = value
        elif key in ("vertex_props", "adjacency", "index"):
            out[key] = word(value, kind)
        elif key == "adjacency_by_etype_dir":
            out[key] = {e: {d: word(v, kind) for d, v in dirs.items()} for e, dirs in value.items()}
        else:
            out[key] = {n: word(v, kind) for n, v in value.items()}
    return out

