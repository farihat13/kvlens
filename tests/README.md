# tests/

The repo's checks, grouped by what they check. Every check prints its counts (cells, files or cases compared) and
exits non-zero on any failure, and on a check of nothing.

| group | what it checks | how it runs |
|---|---|---|
| `lint/` | the repo's own text: file kinds and sizes, paths from one place, one-way layers, no cell body chosen by the runs CSV's planner_pick, that each lint catches its planted violations, the locations, imports, a clone at another path, ratchets | `just lint`, `just import-all`, `just relocation` |
| `unit/` | one module's rule, on synthetic, demo or committed data: plans and the plan IR, optimizer rules, names, schema paths, the fetch and pin tools | `just tests-fast` (the store-free ones), or by hand |
| `gates/` | the C++ evaluator against the Python oracle, golden counts and pricing, the tally version (`just tally-version`), write effects and counts, the configurable format's grid (`gates/meta/`), the layout studies (`gates/layout_choice/`, `gates/mix_benefit/`), and the helpers the gate drivers call | `just golden-counts`, `just tally-version`, `just paper`, `run/meta-gates.sh`, the study drivers; all of them before a merge: `just gate full` (`check_gate_full.py` checks its own rules) |
| `fixtures/` | committed inputs: the golden counts and pricing, a differential snapshot, engine plans | read by the checks above |

`fast_tests.txt` lists the store-free checks across the groups (they need no data and no stores, only the built
binary); `run_fast_tests.py` runs them (`just tests-fast`, part of `just check`).

The C++ unit tests live beside their code, in `cpp/kvevaluator/tests/` (ctest; `just ctest`, also part of `just check`).

## How a check is reached

Every check runs somewhere, and `tests/lint/check_tests_reached.py` (in `just lint`) refuses one that does not:

1. **A runner names it** on a line that is not a comment: `fast_tests.txt`, the justfile, `run/` (the sf0.1 round,
   `run/meta-gates.sh`, and the other gate drivers) or a study's `experiments/<study>/run-*.sh`.
2. **`by_hand.txt` lists it**, for a check too slow or too input-heavy for a round. Each line names the check, its
   trigger (path globs: the change that must run it), its inputs and cost. `just tests-triggered <base>` prints the
   by-hand checks a change since `<base>` calls for; run them before that change merges.
3. **A helper** (a .py whose name does not start with `check_`) is reached by being imported by another file.

The lint also refuses a `by_hand.txt` line whose check is gone, or that a runner already names, and a trigger glob that
matches no file.

## Running one check

From the repo root, in its environment (`just setup`):

    uv run --locked python tests/unit/check_planir.py
    uv run --locked python tests/gates/check_cpp_vs_python.py --help

A check that needs stores or data says so in its docstring (`--help`), with the command that makes them
(`tests/gates/gate_preflight.py --prep` for the gates' stores, `just fetch-dataset`, `just fetch-results`).

## Adding a check

Put it in the group that says what it checks; a helper goes beside the checks that import it (they import each other
by bare name, so a check and its helper share a dir). A store-free check also goes in `fast_tests.txt`; a check that
needs stores goes in its round (`run/meta-gates.sh` for the sf0.1 round) or a study's driver; one run only at a
named moment goes in `by_hand.txt` with its trigger.
