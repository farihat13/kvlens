#!/usr/bin/env python3
"""kvcost's --help says each command's own things: a description that opens with "kvcost <command>" names the command
it is on (predict-accuracy showed predict's), and a flag's "(required but with --X)" names a flag the command has
(cost-layouts' --workload named search-mix's --question). Builds the parser only (kvcost.cli.cli.parser); store-free.

    python3 tests/unit/check_cli_help.py
"""
import argparse
import re
import sys

from kvcost.cli import cli


def main():
    sub = next(x for x in cli.parser()._actions if isinstance(x, argparse._SubParsersAction))
    cases = []
    for name, p in sorted(sub.choices.items()):
        m = re.match(r"kvcost ([a-z-]+)", p.description or "")
        cases.append((f"{name}: its description names {m.group(1) if m else 'no command'}", not m or m.group(1) == name))
        flags = {o for a in p._actions for o in a.option_strings}
        for a in p._actions:
            for other in re.findall(r"required but with (--[a-z-]+)", a.help or ""):
                cases.append((f"{name} {a.option_strings[0]}: 'required but with {other}' names its own flag",
                              other in flags))
    fails = 0
    for label, ok in cases:
        if not ok:
            print(f"FAIL {label}")
        fails += not ok
    print(f"check_cli_help: {len(sub.choices)} commands, {len(cases)} cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
