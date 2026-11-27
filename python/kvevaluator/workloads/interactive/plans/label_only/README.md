# The label_only plan files: frozen

These 13 files are the parked label_only family's plans (the index-free design: every type filter reads the label,
no value or date index). They are kept as committed and are not regenerated:

- The family is parked: `label_only_plan_for` refuses, and no gate, grid or driver runs these files.
- Their bodies were chosen by v1's plan search (the `CRITERION` file names its pick rule). That search and its
  picks file are not in this repo; `plan_choice/dump_planir.py` does not write this directory, and its drift check
  skips it (`tests/unit/check_plan_files.py`).

To bring the family back, generate its plans with `plan_choice/generate.py` (every candidate body) and record its picks in
`inputs/picks/picks.csv`, as the canonical family does.
