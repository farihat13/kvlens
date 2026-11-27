#!/usr/bin/env python3
"""The shared data files (inputs/contract/*.json, which C++ compiles in and Python reads) are well formed, and Python
names no KV op the table lacks. Checked: every entry holds only the keys its file defines (a misspelled key would be
read as absent); every op's kind, args, part, unit and `requires` (validity capabilities of the vocabulary); every
capability's class and `implies`; every capture group, sequence and file kind resolves, and a file kind names a column
once; and every collection of op names in the repo's Python (the files the lints scan: tests/lint/repo_files.py) -- two or more table ops in one literal set, tuple, list or
dict -- holds only table ops. No binary, no store; seconds.

Run:  python3 tests/unit/check_contract_files.py [--plant unknown-cap|dup-column|retired-op|misspelled-key|selector-required]
      (a plant must FAIL)
"""
import argparse
import ast
import copy
import importlib.util
import re
import sys

from kvevaluator import contract
from kvevaluator.paths import ROOT

_spec = importlib.util.spec_from_file_location("repo_files", ROOT / "tests" / "lint" / "repo_files.py")
_repo_files = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repo_files)

PARTS = {"adjacency", "vertex_props", "index"}
UNIT_OF = {"edge_type", "vertex_label"}
OP_LIKE = re.compile(r"^(read_|index_range_|insert_|update_|delete_)")   # what a KV op's name looks like
NOT_OPS = {"read_bytes"}                                                  # a name of that shape that is a column
# the keys each kind of entry may hold (a key outside them is a misspelling the readers would take as absent)
OP_KEYS = {"name", "kind", "every_format", "args", "refusals", "part", "unit", "requires"}
ARG_KEYS = {"name", "required", "default"}
CAP_KEYS = {"class", "implies", "store_derived", "stamped_at_emit", "doc", "evidence"}
FILE_KEYS = {"kv_ops": {"about", "ops"},
             "capabilities": {"about", "chunk_coarse", "chunk_coarse_doc", "last_chunk_fill", "last_chunk_fill_doc", "class_doc",
                              "capabilities"},
             "capture_columns": {"about", "groups", "response_keys", "sequences", "files"}}


def key_problems(files):
    """keys an entry may not hold, per file (`files`: name -> the parsed file)"""
    out = []
    for name, f in files.items():
        out += [f"{name}.json: unknown top-level key {k!r}" for k in sorted(set(f) - FILE_KEYS[name])]
    for o in files["kv_ops"].get("ops", []):
        out += [f"kv_ops.json {o.get('name')}: unknown key {k!r}" for k in sorted(set(o) - OP_KEYS)]
        for a in o.get("args", []):
            out += [f"kv_ops.json {o.get('name')}.{a.get('name')}: unknown key {k!r}" for k in sorted(set(a) - ARG_KEYS)]
    for c, e in files["capabilities"].get("capabilities", {}).items():
        out += [f"capabilities.json {c}: unknown key {k!r}" for k in sorted(set(e) - CAP_KEYS)]
    return out


def op_problems(ops, caps):
    """what is wrong in kv_ops.json's ops (`caps`: capabilities.json's entries)"""
    vocab = {c for c, e in caps.items() if e.get("class") == "validity"}   # what a body's op can require
    out, seen = [], set()
    for o in ops:
        n = o.get("name")
        if n in seen:
            out.append(f"{n}: listed twice")
        seen.add(n)
        if o.get("kind") not in ("read", "write") or not isinstance(o.get("every_format"), bool):
            out.append(f"{n}: kind must be read or write, every_format a bool")
        args = [a["name"] for a in o.get("args", [])]
        if len(set(args)) != len(args):
            out.append(f"{n}: an argument named twice")
        for a in o.get("args", []):
            if a.get("required") and "default" in a:
                out.append(f"{n}.{a['name']}: a required argument has no default")
        if o.get("kind") == "read":
            if o.get("part") not in PARTS:
                out.append(f"{n}: part {o.get('part')!r} not one of {sorted(PARTS)}")
            unit = o.get("unit")
            if unit is not None and (unit.get("of") not in UNIT_OF or unit.get("arg") not in args):
                out.append(f"{n}: unit {unit} must name one of its args, of {sorted(UNIT_OF)}")
            bad = set(o.get("requires", [])) - vocab
            if bad:
                out.append(f"{n}: requires {sorted(bad)}, not validity capabilities of the vocabulary")
        elif {"part", "unit", "requires"} & set(o):
            out.append(f"{n}: a write carries no part, unit or requires")
    return out


def cap_problems(caps):
    """what is wrong in capabilities.json"""
    out = []
    for c, e in caps.items():
        if e.get("class") not in ("validity", "selector"):
            out.append(f"{c}: class must be validity or selector")
        for i in e.get("implies", []):
            if caps.get(i, {}).get("class") != "validity":
                out.append(f"{c}: implies {i!r}, which is not a validity capability")
    return out


def columns_of(cc, name):
    """contract.columns over `cc` (the file, or a planted copy of it)"""
    if name in cc["groups"]:
        return list(cc["groups"][name])
    if name in cc["response_keys"]:
        return list(cc["response_keys"][name])
    seq = cc["sequences"].get(name, cc["files"].get(name))
    if seq is None:
        raise KeyError(f"no group, sequence or file {name!r}")
    return [c for part in seq for c in columns_of(cc, part)]


def column_problems(cc):
    """what is wrong in capture_columns.json: a name defined twice, a name that does not resolve, a column named twice"""
    out = []
    kinds = [set(cc["groups"]), set(cc["response_keys"]), set(cc["sequences"]), set(cc["files"])]
    for i, a in enumerate(kinds):
        for b in kinds[i + 1:]:
            if a & b:
                out.append(f"defined twice: {sorted(a & b)}")
    for kind in cc["files"]:
        try:
            cols = columns_of(cc, kind)
        except KeyError as e:
            out.append(f"file {kind}: {e}")
            continue
        dup = sorted({c for c in cols if cols.count(c) > 1})
        if dup:
            out.append(f"file {kind}: names {dup} twice")
    return out


def stray_op_names(table_ops, extra=None):
    """([(file:line, name)] for each op-shaped name the table lacks, in a literal collection holding two or more table
    ops, the number of such collections)"""
    out, n = [], 0
    sources = [(f, (ROOT / f).read_text()) for f in _repo_files.repo_files(ROOT) if f.endswith(".py")]
    if extra:
        sources.append(extra)
    for f, text in sources:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
                elts = node.elts
            elif isinstance(node, ast.Dict):
                elts = [k for k in node.keys if k is not None]
            else:
                continue
            names = [e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if sum(x in table_ops for x in set(names)) < 2:
                continue
            n += 1
            out += [(f"{f}:{node.lineno}", x) for x in names
                    if OP_LIKE.match(x) and x not in table_ops and x not in NOT_OPS]
    return out, n


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plant", choices=("unknown-cap", "dup-column", "retired-op", "misspelled-key", "selector-required"),
                    help="a control: one deliberate defect (a FAIL is the expected verdict)")
    a = ap.parse_args()
    files = {name: copy.deepcopy(contract.load(name)) for name in FILE_KEYS}
    ops, caps, cc = files["kv_ops"]["ops"], files["capabilities"]["capabilities"], files["capture_columns"]
    extra = None
    if a.plant:
        print(f"=== CONTROL: plant {a.plant}: a FAIL is the expected verdict")
    if a.plant == "unknown-cap":
        ops[0]["requires"] = ["no_such_capability"]
    elif a.plant == "dup-column":
        cc["files"]["steps"] = cc["files"]["steps"] + ["shape"]
    elif a.plant == "misspelled-key":
        caps["property_index"]["stamped_at_emmit"] = caps["property_index"].pop("stamped_at_emit")
    elif a.plant == "selector-required":
        ops[0]["requires"] = ["whole_row_read"]
    elif a.plant == "retired-op":
        extra = ("<plant>", 'X = {"read_neighbors", "read_chase", "read_adj_window_by_date"}\n')
    problems = key_problems(files) + op_problems(ops, caps) + cap_problems(caps) + column_problems(cc)
    stray, ncoll = stray_op_names({o["name"] for o in ops}, extra)
    problems += [f"{at}: names {x!r}, not an op of inputs/contract/kv_ops.json" for at, x in stray]
    print(f"[contract] {len(ops)} ops, {len(caps)} capabilities, {len(cc['files'])} capture file kinds; "
          f"{ncoll} Python op collections scanned")
    if not ops or not caps or not ncoll:
        problems.append("0 ops, capabilities or op collections checked")
    for p in problems:
        print("  FAIL", p)
    print(f"contract files {'PASS' if not problems else 'FAIL'}: {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
