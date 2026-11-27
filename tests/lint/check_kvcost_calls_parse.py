#!/usr/bin/env python3
"""Every kvcost call written in the repo parses with kvcost's own parser (kvcost.cli.cli.parser()); nothing runs.

The calls, by kind:
  chain     run/reproduce.py's figure chains: every figure's `results` steps, at the paper's scales and at the smallest
  question  each mix question's search-mix calls, as `kvcost search-mix --question` expands them (mix_questions.json)
  shell     each command line that runs kvcost in a shell script or the justfile, and each kvcost call a message
            names (a quoted string); a comment is not read
  python    each argv that runs kvcost in a Python file (read with ast; nothing imported): a list naming kvcost.cli.cli
            and a command, or, outside python/, a list or a helper call's arguments that start with a command and a flag
  doc       each code span and code-block line of a .md file that starts a kvcost call
A word the source fills in at run time ($SF, <SF>, an f-string, a variable) stands for a valid value of its flag; `...`
and an expansion that is not a flag's value are dropped, and an optional `[...]` is read as written. A call written in
part (built in pieces, or shortened in a doc) may leave out a required flag; a chain's or a question's may not.
Exit 1 on any call that does not parse (file:line, the call and the parser's message), or when a kind parsed no call.

    python tests/lint/check_kvcost_calls_parse.py [--list]
"""
import argparse
import ast
import importlib.util
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from repo_files import repo_files, is_text
from kvevaluator import datasets, sizes
from kvevaluator.paths import ROOT

TAG = "kvcost-call:"


class ParseError(Exception):
    pass


def _error(self, message):
    raise ParseError(message)


_PARSER = {}


def commands():
    """{command: its subparser}, from the one parser"""
    if not _PARSER:
        from kvcost.cli.cli import parser
        ap = parser()
        sub = next(x for x in ap._actions if isinstance(x, argparse._SubParsersAction))
        _PARSER.update(ap=ap, cmds=dict(sub.choices))
    return _PARSER["cmds"]


def _accepts(action, value):
    """True when the action's type takes the value"""
    if action.type is None:
        return True
    try:
        action.type(value)
        return True
    except Exception:
        return False


def _sample(action, taken):
    """a value the action accepts, for a word the source fills in at run time; a --dataset hole takes a dataset=scale
    the call does not name yet (a pair named twice is refused)"""
    if "--dataset" in action.option_strings:
        return next(v for v in (f"{d}={sc}" for d in datasets.names() for sc in datasets.get(d).scales)
                    if v not in taken)
    if action.choices:
        return str(next(iter(action.choices)))
    return next((v for v in ("1", "X") if _accepts(action, v)), "X")


def _positional_sample(action):
    """a value the positional accepts: a layout name for a layout list, else a plain word"""
    return next((v for v in ("Sp-Co", "best", "X") if _accepts(action, v)), "X")


HOLE = object()


def fill(cmd, words):
    """the argv of one call: words are str or (HOLE, text); a hole after a flag that takes a value gets a value that
    flag accepts, a hole elsewhere a positional's value (dropped when the command takes none)"""
    p = commands()[cmd]
    opts = {s: x for x in p._actions for s in x.option_strings}
    positionals = [x for x in p._actions if not x.option_strings]
    out, expecting, filled = [], None, 0
    taken = {w for x, w in zip(words, words[1:]) if x == "--dataset" and isinstance(w, str)}
    for w in words:
        if isinstance(w, tuple):
            if expecting is not None:
                v = _sample(expecting, taken)
                taken.add(v)
                out.append(v)
                expecting = None
            elif filled < len(positionals) and not re.search(r"\[@\]|\$@|\$\*", w[1]):
                out.append(_positional_sample(positionals[filled]))
                filled += positionals[filled].nargs in (None, 1)
                filled = len(positionals) if filled == 0 else filled   # a list positional takes the holes once
            continue
        out.append(w)
        expecting = opts[w] if (w in opts and opts[w].nargs != 0) else None
    if expecting is not None:             # a flag named at the end, as a doc mentions it: give it a value
        out.append(_sample(expecting, taken))
    return out


def parse(cmd, argv, strict):
    """None when [cmd, *argv] parses, else the parser's message; strict=False lets a required flag be left out"""
    if any(x in ("-h", "--help") for x in argv):
        return None
    # not strict: no flag is required, so an unknown flag is found even where a required one is left out (argparse
    # reports the missing one first)
    lifted = [x for x in commands()[cmd]._actions if x.required and not strict]
    msg = None
    with mock.patch.object(argparse.ArgumentParser, "error", _error), redirect_stderr(StringIO()), \
            redirect_stdout(StringIO()):
        for x in lifted:
            x.required = False
        try:
            _PARSER["ap"].parse_args([cmd, *argv])
        except ParseError as e:
            msg = str(e)
        except SystemExit as e:
            msg = f"exits: {e}"
        except Exception as e:          # an action that raises: the call does not parse
            msg = f"{type(e).__name__}: {e}"
        finally:
            for x in lifted:
                x.required = True
    return msg


# --- reading calls out of text ------------------------------------------------------------------------------------

def _start(verbs, arrays=()):
    """a kvcost call's start: `kvcost <command>`, `-m kvcost.cli.cli <command>`, or a shell array that holds kvcost's
    command (`"${K[@]}" <command>`), as a word of its own"""
    alts = "|".join(sorted(map(re.escape, verbs), key=len, reverse=True))
    arr = "".join(rf"|\$\{{{a}\[@\]\}}\"?" for a in arrays)
    return re.compile(r"(?:^|(?<=[\s\"'`(]))(?:kvcost\.cli\.cli|kvcost" + arr + r")\s+(" + alts + r")(?=$|[\s\"'`;)|&])")


KV_ARRAY = re.compile(r"^\s*(?:local\s+)?(\w+)=\([^)]*kvcost\.cli\.cli[^)]*\)", re.M)   # K=("$PYTHON" -m kvcost.cli.cli)


METAVAR = re.compile(r"[A-Z][A-Z_]*(=[A-Z][A-Z_]*)?")   # a doc's D=S, W, DIR: a word the reader fills in
COND = re.compile(r"\$\{\w+:\+([^}]*)\}")     # ${VAR:+words}: the words, as the call would hold them


def words_of(text, doc):
    """the words of one call's text after its command, up to the shell's end of the command: (HOLE, text) for a word the
    source fills in ($, `, <...>, and in a doc a|b), with quotes taken off; doc=True: a doc's call (`...` dropped,
    [ and ] taken off, | and < > end nothing but a lone |)"""
    text = COND.sub(lambda m: m.group(1), text)
    out, cur, quote, depth, hole, i, stop = [], [], None, 0, False, 0, False

    def end_word():
        nonlocal cur, hole, stop
        if cur or hole:
            w = "".join(cur)
            if doc:
                w = w.strip("[]")
                if w[-1:] in ",:" or (w.endswith(")") and "(" not in w):
                    w, stop = w[:-1], True        # prose after a call in a message: the call ends at its comma
            if w in ("...", "…") or (doc and w.endswith("...")):
                pass
            elif hole or (doc and (re.search(r"<[^>]*>", w) or ("|" in w and w != "|") or METAVAR.fullmatch(w))):
                out.append((HOLE, w))
            elif w:
                out.append(w)
        cur, hole = [], False

    while i < len(text):
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "$" or c == "`":
                hole = True
            elif c == "\\" and quote == '"' and i + 1 < len(text):
                i += 1
                cur.append(text[i])
            else:
                cur.append(c)
        elif depth:
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            cur.append(c)
        elif doc and c == "<" and not cur and ">" in text[i:]:
            j = text.index(">", i)                 # a doc's <placeholder>, spaces and all: one word to fill in
            cur.append(text[i:j + 1])
            hole = True
            i = j
        elif c in "'\"":
            quote = c
        elif c == "$":
            hole = True
            if text[i + 1:i + 2] in ("(", "{"):
                depth = 1
                i += 1
        elif c == "`":
            hole = True
        elif c.isspace():
            end_word()
            if stop:
                break
        elif c == "#" and not cur:
            break
        elif doc and c in "()" and not cur:
            break
        elif not doc and c in ";|&<>)":
            break
        elif doc and c == "|" and not cur and (i + 1 == len(text) or text[i + 1].isspace()):
            break
        elif doc and c == ";":
            break
        else:
            cur.append(c)
        i += 1
    end_word()
    return out


def joined_lines(text):
    """(first line number, logical line) with backslash continuations joined"""
    out, buf, first = [], [], None
    for n, line in enumerate(text.splitlines(), 1):
        if first is None:
            first = n
        if line.rstrip().endswith("\\"):
            buf.append(line.rstrip()[:-1])
            continue
        buf.append(line)
        out.append((first, " ".join(buf)))
        buf, first = [], None
    if buf:
        out.append((first, " ".join(buf)))
    return out


def _code_and_quotes(line):
    """(the line without its comment, [the quote open at each character]): a # that starts a word outside quotes
    starts the comment"""
    quote, opened = None, []
    for i, c in enumerate(line):
        if quote is None and c == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i], opened
        opened.append(quote)
        if quote is None and c in "'\"" and (i == 0 or line[i - 1] != "\\"):
            quote = c
        elif c == quote and (i == 0 or line[i - 1] != "\\"):
            quote = None
    return line, opened


def shell_calls(text):
    """[(line, command, words)] of a shell script: each command line (not a comment) that runs kvcost; a call written
    inside a quoted string (a message naming the command to run) is read as a doc's call, up to the closing quote"""
    start = _start(commands(), KV_ARRAY.findall(text))
    out = []
    for n, line in joined_lines(text):
        code, opened = _code_and_quotes(line)
        for m in start.finditer(code):
            q = opened[m.start()]
            if q:
                rest = code[m.end():]
                out.append((n, m.group(1), words_of(rest[:rest.index(q)] if q in rest else rest, doc=True)))
            else:
                out.append((n, m.group(1), words_of(code[m.end():], doc=False)))
    return out


def doc_calls(text):
    """[(line, command, words)] of a .md file: each kvcost call in a code span or a code-block line"""
    start = _start(commands())
    out, fence = [], None
    lines = text.splitlines()
    spans = []
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith(("```", "~~~")):
            fence = None if fence else s[:3]
            continue
        if fence:
            spans.append((n, line))
        else:
            spans += [(n, m.group(1)) for m in re.finditer(r"`([^`]+)`", line)]
    # a code block's call may continue over lines ending in a backslash
    merged = []
    for n, s in spans:
        if merged and merged[-1][1].rstrip().endswith("\\") and merged[-1][2] == n - 1:
            merged[-1] = (merged[-1][0], merged[-1][1].rstrip()[:-1] + " " + s, n)
        else:
            merged.append((n, s, n))
    for n, s, _ in merged:
        for m in start.finditer(s):
            out.append((n, m.group(1), words_of(s[m.end():], doc=True)))
    return out


def _flat(node, prefixes):
    """the elements of a list expression: (node, str) for a string, (node, HOLE) for a value filled at run time,
    (node, None) for a starred list not known here; None when the expression is not a list"""
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                out.append((e, e.value))
            elif isinstance(e, ast.Starred):
                inner = prefixes.get(e.value.id) if isinstance(e.value, ast.Name) else _flat(e.value, prefixes)
                out += inner if inner is not None else [(e, None)]
            else:
                out.append((e, HOLE))
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _flat(node.left, prefixes), _flat(node.right, prefixes)
        if a is None or b is None:
            return (a or []) + [(node.right, None)] if a is not None else None
        return a + b
    if isinstance(node, ast.Name) and node.id in prefixes:
        return prefixes[node.id]
    return None


def _kv_at(elts):
    """the index of the kvcost word in a list's elements, or None"""
    return next((i for i, (_, v) in enumerate(elts) if v in ("kvcost.cli.cli", "kvcost")), None)


def _command_at(elts, verbs):
    """the index of a kvcost command in a sequence's elements: after the kvcost word, or, in a sequence that names no
    other module, a command name followed by a flag (an argv a helper hands to kvcost: kv("predict", "--workload", ..))"""
    k = _kv_at(elts)
    if k is not None:
        return k + 1 if k + 1 < len(elts) and elts[k + 1][1] in verbs else None
    if any(isinstance(v, str) and (v == "-m" or v.endswith(".py")) for _, v in elts):
        return None
    return next((i for i, (_, v) in enumerate(elts[:-1]) if v in verbs and isinstance(elts[i + 1][1], str)
                 and elts[i + 1][1].startswith("--")), None)


def python_calls(text, named_only=False):
    """[(line, command, words)] of a Python file that names kvcost: each argv that runs kvcost (a list naming
    kvcost.cli.cli and a command, one joined to a name bound to such a list, or a list, tuple or call's arguments that
    start with a command and a flag)"""
    if "kvcost" not in text:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    verbs = commands()
    prefixes = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            elts = _flat(node.value, prefixes)
            if elts and _kv_at(elts) is not None:
                prefixes[node.targets[0].id] = elts
    out, seen = [], set()
    for node in ast.walk(tree):
        if id(node) in seen:
            continue
        if isinstance(node, ast.Call):
            elts = _flat(ast.List(elts=node.args), prefixes)
        elif isinstance(node, (ast.List, ast.Tuple, ast.BinOp)):
            elts = _flat(node, prefixes)
        else:
            continue
        i = _command_at(elts, verbs) if elts and (not named_only or _kv_at(elts) is not None) else None
        if i is None:
            continue
        seen |= {id(x) for x in ast.walk(node)}
        words = [(HOLE, "") if v is HOLE else v for _, v in elts[i + 1:] if v is not None]
        out.append((node.lineno, elts[i][1], words))
    return out


KINDS = {"shell": shell_calls, "python": python_calls, "doc": doc_calls}


def kind_of(rel):
    if rel.endswith(".md"):
        return "doc"
    if rel.endswith(".sh") or rel == "justfile":
        return "shell"
    if rel.endswith(".py"):
        return "python"
    return None


def scan(root, counts=None, listed=None):
    """(problems, files read): every kvcost call in the root's .sh, justfile, .py and .md files parses. counts gets
    {kind: calls parsed}"""
    root = Path(root)
    counts = {} if counts is None else counts
    problems, n = [], 0
    for rel in repo_files(root):
        kind = kind_of(rel)
        if kind is None or rel.startswith(("engines/", "python/kvengines/", "data/", "output/", "build/")):
            continue
        path = root / rel
        if not is_text(path):
            continue
        text = path.read_text(errors="replace")
        n += 1
        found = (python_calls(text, named_only=rel.startswith("python/")) if kind == "python" else KINDS[kind](text))
        for line, cmd, words in found:
            counts[kind] = counts.get(kind, 0) + 1
            argv = fill(cmd, words)
            if listed is not None:
                listed.append(f"{kind} {rel}:{line}: {cmd} {' '.join(argv)}")
            msg = parse(cmd, argv, strict=False)
            if msg is None:
                continue
            shown = " ".join(w if isinstance(w, str) else (w[1] or "<filled>") for w in words)
            problems.append(f"{rel}:{line}: {TAG} kvcost {cmd} {shown[:160]}: {msg}")
    return problems, n


# the paper's scales with LDBC at 30 beside 3: the rows the paper does not report, still run by flag
LARGER = sizes.PAPER.replace("ldbc=3", "ldbc=3,ldbc=30")


def chain_calls():
    """[(where, command, argv)] of the paper's figure chains (run/reproduce.py), at the paper's scales, with LDBC SF30
    beside them (LARGER), and at each dataset's smallest"""
    spec = importlib.util.spec_from_file_location("reproduce", ROOT / "run" / "reproduce.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    small = sizes.resolve(",".join(f"{d}={datasets.get(d).scales[0]}" for d in datasets.names()))
    out = []
    for run in (sizes.resolve(sizes.PAPER), sizes.resolve(LARGER), small):
        for name, f in rp.FIGURES.items():
            if f["results"] is None:          # an archive-only item: no chain, no kvcost call
                continue
            # an item with scales of its own (its registry flags name another --dataset than the paper's) has its
            # chain at those
            own = f["flags"].get("dataset", sizes.PAPER)
            at = sizes.resolve(own) if own != sizes.PAPER and run is not small else run
            for step in f["results"](f["flags"].get("workload"), at):
                if step[:len(rp.KV)] == rp.KV:
                    out.append((f"run/reproduce.py results {name} --dataset {sizes.text(at)}", step[len(rp.KV)],
                                step[len(rp.KV) + 1:]))
    return out


def question_calls():
    """[(where, command, argv)] of every mix question's search-mix calls, as --question expands them, at the paper's
    scales with LDBC SF30 beside them (LARGER: a question on a larger scale searches SF30)"""
    from kvcost.cli import search as SR
    from kvcost.layout_search import mix_questions as MQ
    run = sizes.resolve(LARGER)
    out = []
    for name in MQ.questions():
        calls = SR.question_calls(argparse.Namespace(question=name, run=run, from_dir=None, fresh=False))
        out += [(f"{MQ.TABLE.relative_to(ROOT)} {name}", "search-mix", c[0]) for c in calls if c[0] != "local-search"]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print every call as parsed (holes filled), then the verdict")
    a = ap.parse_args(argv)
    counts = {}
    problems, listed = [], ([] if a.list else None)
    for kind, calls in (("chain", chain_calls()), ("question", question_calls())):
        for where, cmd, args in calls:
            counts[kind] = counts.get(kind, 0) + 1
            if listed is not None:
                listed.append(f"{kind} {where}: {cmd} {' '.join(args)}")
            msg = parse(cmd, args, strict=True)
            if msg is not None:
                problems.append(f"{where}: {TAG} kvcost {cmd} {' '.join(args)[:160]}: {msg}")
    more, n = scan(ROOT, counts, listed)
    problems += more
    for x in listed or []:
        print(x)
    for p in problems:
        print(p)
    empty = [k for k in ("chain", "question", "shell", "python", "doc") if not counts.get(k)]
    for k in empty:
        print(f"{k}: no call parsed (a check that covers nothing has failed)")
    shown = ", ".join(f"{k} {counts.get(k, 0)}" for k in ("chain", "question", "shell", "python", "doc"))
    print(f"check_kvcost_calls_parse {'FAIL' if problems or empty else 'OK'}: {sum(counts.values())} calls in {n} "
          f"files ({shown}); {len(problems)} do not parse")
    return 1 if problems or empty else 0


if __name__ == "__main__":
    sys.exit(main())
