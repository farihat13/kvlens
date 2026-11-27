"""Duplicate code, as a ratchet: lines of python/, cpp/kvevaluator/src and experiments/ that sit in a 12-line window
found in another file.

A line is normalized (whitespace, comments, docstrings, names, numbers and strings dropped to a placeholder; keywords and
punctuation kept). Lines holding only brackets, imports, includes or one table element are skipped. A window is 12 skipped-free lines in a
row; a window found in two or more files marks its lines as duplicated. The count is those lines, per package
(duplicate_code.json beside this file); a package whose count rises fails, a lower count prints a note. A copy inside
one file is not counted. No dependency: a plain token hasher, about 2 s.

    python tests/lint/check_duplicate_code.py              # exit 1 if a package's count rose
    python tests/lint/check_duplicate_code.py --top 20     # the 20 longest duplicated blocks, file:line to file:line
    python tests/lint/check_duplicate_code.py --window 8   # another window (the baseline is for 12)
    python tests/lint/check_duplicate_code.py --write --reason "why the count moved"
"""
import argparse
import datetime
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from kvevaluator.paths import ROOT
from repo_files import repo_files

BASELINE = Path(__file__).with_name("duplicate_code.json")
WINDOW = 12
SCOPE = ("python/", "cpp/kvevaluator/src/", "experiments/")
KEYWORDS = {
    ".py": set("and as assert break class continue def del elif else except finally for from global if import in is lambda "
               "nonlocal not or pass raise return try while with yield None True False self".split()),
    ".cc": set("auto bool break case catch char class const constexpr continue default delete do double else enum explicit "
               "false float for if inline int namespace new nullptr operator private public return size_t static struct "
               "switch template this throw true try typedef uint32_t uint64_t uint8_t unsigned using virtual void while "
               "std string vector map set".split()),
}
KEYWORDS[".h"] = KEYWORDS[".cc"]
# one token each, in order: a docstring (Python), a string, a comment, a newline, a name, a number, any other character
COMMENT = {".py": r"\#[^\n]*", ".cc": r"//[^\n]*|/\*.*?\*/"}
DOC = {".py": r"\"\"\".*?\"\"\"|'''.*?'''", ".cc": r"(?!)"}
STRING = r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
TOKENS = {sfx: re.compile(rf"(?P<doc>{DOC[sfx]})|(?P<str>{STRING})|(?P<comment>{COMMENT[sfx]})|(?P<nl>\n)"
                          rf"|(?P<name>[A-Za-z_]\w*)|(?P<num>\d[\w.]*)|(?P<other>\S)", re.S)
          for sfx in (".py", ".cc")}
TOKENS[".h"] = TOKENS[".cc"]
SKIP_LINE = re.compile(r"^[\s)\]}{(\[,;:]*$|^(import|from) |^#\s*(include|pragma|ifndef|define|endif)")
BARE_ITEM = re.compile(r"^(S|N|0)( ,)?$")    # one element of a table or list: data, not logic


def normalized_lines(text, suffix):
    """[(line number, normalized line)] for the lines that count: names, numbers and strings become N, 0, S"""
    keywords, out, line_no, cur, raw = KEYWORDS[suffix], [], 1, [], []
    for m in TOKENS[suffix].finditer(text):
        kind, tok = m.lastgroup, m.group()
        if kind == "nl":
            out.append((line_no, " ".join(cur), " ".join(raw)))
            line_no, cur, raw = line_no + 1, [], []
            continue
        if kind in ("comment", "doc"):
            line_no += tok.count("\n")      # a docstring or block comment is prose: no tokens, but its lines pass
            continue
        if kind == "str":
            cur.append("S")
        elif kind == "num":
            cur.append("0")
        elif kind == "name":
            cur.append(tok if tok in keywords else "N")
        else:
            cur.append(tok)
        raw.append(tok)
    out.append((line_no, " ".join(cur), " ".join(raw)))
    return [(n, s) for n, s, raw in out if s and not SKIP_LINE.match(raw.strip()) and not BARE_ITEM.match(s)]


def windows(lines, size):
    """(hash, first line, last line, position) of each run of `size` counted lines in a row"""
    for i in range(len(lines) - size + 1):
        chunk = lines[i:i + size]
        yield hash("\n".join(s for _, s in chunk)), chunk[0][0], chunk[-1][0], i


def find(root, size=WINDOW):
    """({file: [counted lines]}, {file: [(first, last, other file:line)]}, files scanned) of the duplicated blocks"""
    root = Path(root)
    where, per_file = defaultdict(list), {}
    scanned = 0
    for rel in repo_files(root):
        suffix = Path(rel).suffix
        if suffix not in KEYWORDS or not rel.startswith(SCOPE):
            continue
        scanned += 1
        lines = normalized_lines((root / rel).read_text(errors="replace"), suffix)
        per_file[rel] = lines
        for h, first, last, pos in windows(lines, size):
            where[h].append((rel, pos, first))
    covered = defaultdict(set)      # file -> the positions (indexes into its counted lines) in a window found elsewhere
    partner = {}                    # (file, position of a window) -> another file:line holding it
    for h, found in where.items():
        files = {rel for rel, _, _ in found}
        if len(files) < 2:
            continue
        for rel, pos, first in found:
            covered[rel].update(range(pos, pos + size))
            other = next((r, f) for r, _, f in found if r != rel)
            partner[(rel, pos)] = f"{other[0]}:{other[1]}"
    dup_lines, blocks = {}, []
    for rel, positions in covered.items():
        lines = per_file[rel]
        dup_lines[rel] = len(positions)
        run = []
        for p in sorted(positions) + [None]:
            if run and (p is None or p != run[-1] + 1):
                start = next(q for q in range(run[0], run[-1] + 1) if (rel, q) in partner)
                blocks.append((len(run), rel, lines[run[0]][0], lines[run[-1]][0], partner[(rel, start)]))
                run = []
            if p is not None:
                run.append(p)
    blocks.sort(key=lambda b: (-b[0], b[1], b[2]))
    once, seen = [], set()      # a block and its twin in the other file are one duplicate: list it once
    for b in blocks:
        twin = (b[0], frozenset((b[1], b[4].rsplit(":", 1)[0])))
        if twin not in seen:
            seen.add(twin)
            once.append(b)
    return dup_lines, once, scanned


def package_key(rel):
    return rel.split("/")[1] if rel.startswith("python/") else rel.split("/")[0]


def by_package(dup_lines):
    out = defaultdict(int)
    for rel, n in dup_lines.items():
        out[package_key(rel)] += n
    return dict(sorted(out.items()))


def scan(root):
    """(problems, files scanned, {package: duplicated lines}, blocks); one 'path:line: duplicate-code: ...' line per package that rose"""
    dup_lines, blocks, scanned = find(root)
    now = by_package(dup_lines)
    path = Path(root) / "tests" / "lint" / BASELINE.name
    recorded = json.loads(path.read_text())["counts"] if path.is_file() else {}
    problems = []
    for pkg, n in now.items():
        if n > recorded.get(pkg, 0):
            # a block is listed once, under one of its two files: this package's side may be the partner
            worst = []
            for b in blocks:
                if package_key(b[1]) == pkg:
                    worst.append((f"{b[1]}:{b[2]}", f"{b[1]}:{b[2]}-{b[3]} ({b[0]} lines) also at {b[4]}"))
                elif package_key(b[4].rsplit(":", 1)[0]) == pkg:
                    worst.append((b[4], f"{b[4]} ({b[0]} lines) also at {b[1]}:{b[2]}-{b[3]}"))
            worst = worst[:3]
            where = "; ".join(w for _, w in worst)
            problems.append(f"{worst[0][0]}: duplicate-code: {pkg} has {n} lines in blocks found in another "
                            f"file (recorded {recorded.get(pkg, 0)}); longest: {where}. Share the code, or record why: "
                            f"check_duplicate_code.py --write --reason \"...\"")
    return problems, scanned, now, blocks


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--top", type=int, default=0, help="print the N longest duplicated blocks")
    ap.add_argument("--window", type=int, default=WINDOW, help="lines per window (the baseline is for 12)")
    ap.add_argument("--write", action="store_true", help="record today's counts in duplicate_code.json")
    ap.add_argument("--reason", help="with --write: one line, why a count moved (required)")
    a = ap.parse_args()
    if a.window != WINDOW:
        dup_lines, blocks, scanned = find(ROOT, a.window)
        problems, now = [], by_package(dup_lines)
    else:
        problems, scanned, now, blocks = scan(ROOT)
    for size, rel, first, last, other in blocks[:a.top]:
        print(f"{size:4d} lines  {rel}:{first}-{last}  =  {other}")
    if a.write:
        if not a.reason:
            raise SystemExit("check_duplicate_code: --write needs --reason \"<one line: why the count moved>\"")
        old = json.loads(BASELINE.read_text()) if BASELINE.is_file() else {"reasons": []}
        summ = ", ".join(f"{p} {n}" for p, n in now.items())
        reasons = old["reasons"] + [f"{datetime.date.today().isoformat()}: {a.reason} ({summ})"]
        BASELINE.write_text(json.dumps({"counts": now, "reasons": reasons}, indent=1) + "\n")
        print(f"check_duplicate_code: recorded {BASELINE.name}: {summ}")
        problems = []
    recorded = json.loads(BASELINE.read_text())["counts"] if BASELINE.is_file() else {}
    lower = [p for p, n in recorded.items() if now.get(p, 0) < n]
    for p in problems:
        print(p)
    if lower and not a.write:
        print(f"check_duplicate_code: {', '.join(lower)} below the record: lower it (--write --reason \"...\")")
    print(f"check_duplicate_code {'FAIL' if problems else 'OK'}: {len(problems)} problems in {scanned} files (window "
          f"{a.window}); duplicated lines: " + ", ".join(f"{p} {n}" for p, n in now.items()) + f"; {len(blocks)} blocks")
    return 1 if problems or scanned == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
