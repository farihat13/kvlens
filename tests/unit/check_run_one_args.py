"""run_one refuses a bad argument by name before it builds a store: --param 0 (it once ran the last row), an unknown
query, an unknown store name, a body the query does not have. Each exits 1 with its message and leaves the output dir
empty. Needs the binary (run_one reads its named formats); no data, no store.

    python tests/unit/check_run_one_args.py
"""
import os
import subprocess
import sys
import tempfile

CASES = [   # (what, the arguments after --family interactive, a phrase the refusal must hold)
    ("--param 0 is refused", ["--store", "nebula", "--query", "is2", "--param", "0"], "1-based row"),
    ("an unknown query is refused", ["--store", "nebula", "--query", "is99"], "unknown query 'is99'"),
    ("an unknown store is refused with the layout grammar", ["--store", "Xx-Co", "--query", "is2"], "Sp (Split)"),
    ("a body the query lacks is refused", ["--store", "nebula", "--query", "is2", "--body", "nope"],
     "--body 'nope' is not a body of is2"),
]
failures = []
with tempfile.TemporaryDirectory() as out:
    env = dict(os.environ, KVEVAL_OUTPUT_DIR=out)
    for what, args, phrase in CASES:
        r = subprocess.run([sys.executable, "-m", "kvevaluator.measure.run_one", "--sf", "0.1", "--family",
                            "interactive", *args], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        text = r.stdout + r.stderr
        made = sorted(os.listdir(out))
        ok = r.returncode == 1 and phrase in text and "Traceback" not in text and not made
        print(f"{'ok  ' if ok else 'FAIL'} {what}" + ("" if ok else f": rc {r.returncode}, made {made}, said "
                                                    f"{text.strip().splitlines()[-1:] }"))
        if not ok:
            failures.append(what)
print(f"check_run_one_args {'FAIL' if failures else 'OK'}: {len(CASES)} cases, {len(failures)} failed")
sys.exit(1 if failures else 0)
