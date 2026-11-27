# The artifact's commands. `just` lists them. Locations come from python/kvevaluator/paths.py (env or .env).

py := "uv run --locked python"

# List the recipes
default:
    @just --list

# Install the Python packages into .venv: kvevaluator, kvcost, the lint extra (`just lint`'s standard tools), and the engines extra (kvengines and the database client
# packages: the real-database runs and `just cpp-diff`'s engine parts import them)
setup:
    uv sync --locked --extra engines --extra lint
    @echo "setup OK: .venv with kvevaluator $({{py}} -c "import importlib.metadata as m; print(m.version('kvevaluator'))"), kvcost $({{py}} -c "import importlib.metadata as m; print(m.version('kvcost'))")"

# Install what the three databases' builds and servers need on this machine (engines/setup.sh reads engines/needs.tsv;
# Ubuntu 24.04, sudo; `--db dgraph`, `--dry-run`). It builds no database: that is engines/once.sh (engines/README.md)
setup-engines *args: setup
    bash engines/setup.sh {{args}}

# Build the C++ evaluator into the machine's build cache, once per source state, and hard-link it into the build dir
# (run/build_cpp.py; RocksDB is built once per machine). `just build <sha>` builds that commit's binary and prints its path
build *args:
    #!/usr/bin/env bash
    set -euo pipefail
    bash run/check-build-tools.sh      # a missing tool stops here by name (README "Needs"), not as a raw exit 127
    set -- {{args}}
    if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then sha=$1; shift; set -- --sha "$sha" "$@"; fi
    {{py}} run/build_cpp.py "$@"

# The C++ unit tests, in the build that made the binary (its cache entry)
ctest: build
    {{py}} run/build_cpp.py --ctest

# The lints: code only, paths from one place, every kvcost call written in the repo parses, one-way layers and the structure ratchets (big files, environment reads, hand-named workloads, cost formulas), duplicate code, no body chosen by planner_pick, every plotter on the shared style, a body is never a `variant`, and that each lint catches its plants
lint:
    #!/usr/bin/env bash
    # every check runs; the recipe fails at the end and names each check that failed
    checks=(
        tests/lint/check_code_only.py
        tests/lint/check_paths.py
        tests/lint/check_layers.py
        tests/lint/check_duplicate_code.py
        tests/lint/check_lint_tools.py
        tests/lint/check_planner_pick_reads.py
        tests/lint/check_plot_style.py
        tests/lint/check_pick_files_read.py
        tests/lint/check_terms.py
        tests/lint/check_no_research_repo.py
        tests/lint/check_lints_catch_plants.py
        tests/lint/check_locations.py
        tests/lint/check_param_sources.py
        tests/lint/check_tests_reached.py
        tests/lint/check_kvcost_calls_parse.py
        tests/unit/check_fetch_results.py
        tests/unit/check_fetch_ldbc.py
        tests/unit/check_build_tools.py
    )
    failed=()
    for check in "${checks[@]}"; do
        {{py}} "$check" || failed+=("$check")
    done
    if [ ${#failed[@]} -gt 0 ]; then echo "just lint: ${#failed[@]} of ${#checks[@]} checks failed: ${failed[*]}" >&2; exit 1; fi
    echo "just lint: ${#checks[@]} of ${#checks[@]} checks passed"

# The by-hand checks (tests/by_hand.txt) whose triggers match a file changed since <base>: run them before the merge
tests-triggered base="main":
    {{py}} tests/lint/check_tests_reached.py --triggered {{base}}

# Every package module imports with no path tricks
import-all:
    {{py}} -I tests/lint/check_import_all.py

# A clone of HEAD at another path, with a space, under an empty environment: finds its root and imports
relocation:
    tests/lint/check_relocation.sh

# A quick proof the install works: imports, the binary builds and runs
smoke: build
    #!/usr/bin/env bash
    set -euo pipefail
    out=$({{py}} -I tests/lint/check_import_all.py); echo "$out"
    n=$(sed -n 's/.*OK: \([0-9]*\) imported.*/\1/p' <<< "$out")
    v=$({{py}} -c "import importlib.metadata as m; print(m.version('kvevaluator'))")
    b=$("$({{py}} -m kvevaluator.paths cpp_bin)" --version | {{py}} -c "import json, sys; d = json.load(sys.stdin); print(d['binary_git_sha'][:10] + ', dirty ' + str(d['binary_dirty']).lower())")
    echo "smoke OK: $n modules, kvevaluator $v (binary $b)"

# The store-free tests (tests/fast_tests.txt): no data or stores, only the built binary; about 1.5-5 min
tests-fast: build
    {{py}} tests/run_fast_tests.py

# The fast tests that need no C++ binary (CI builds none): skips the ones tagged `binary:` in tests/fast_tests.txt
tests-fast-no-binary:
    {{py}} tests/run_fast_tests.py --no-binary

# Costs equal the freeze's: Sp-Co composed and costed on a fixed 2-cell capture (no store, no binary; seconds)
golden-costs:
    {{py}} tests/gates/check_golden_costs.py

# What every change passes
check: lint import-all relocation ctest tests-fast golden-costs
    @echo "check OK: 6 parts (lint import-all relocation ctest tests-fast golden-costs)"

# The reuse ledger (inputs/adoptions.json): the rows that declare an earlier result current for today's code, one line
# each (`just adoptions check` validates them; `--kind K` lists one kind)
adoptions *args="list":
    {{py}} -m kvevaluator.adoptions {{args}}

# `full` is every check a merge needs on this tree's clean commit, and writes the pass record the merge check requires.
# The checks: `just gate quick` = just check (~4.5 min, per commit); `just gate full` = run/gate-full.sh (~18 min);
# `just gate analysis` = build and check with a record the merge check takes only for a branch that changes nothing
# but paper/, docs/ and Markdown (~5 min; run/gate_levels.py); `just gate analysis-code` = build, check and the round's
# gates analysis code can move, on a copy of a full gate's capture with the same key, for a branch that changes nothing
# but those and python/kvcost, experiments/, tests/unit/ (~12 min; ~18 with no capture to reuse)
gate kind:
    #!/usr/bin/env bash
    case "{{kind}}" in
      quick)    exec just check ;;
      full)     exec run/gate-full.sh ;;
      analysis) exec run/gate-full.sh --level analysis ;;
      analysis-code) exec run/gate-full.sh --level analysis-code ;;
      *)        echo "usage: just gate quick|full|analysis|analysis-code" >&2; exit 2 ;;
    esac

# Put the paper's results (paper-results/, one output tree) at the repo's root: the results repo at the commit
# inputs/paper_results.json pins, every file checked by sha256; --source DIR|URL another copy of that repo; --check
# checks the tree in place (see run/fetch_results.py)
fetch-results *args:
    python3 run/fetch_results.py {{args}}

# Pin paper-results/ (a clone of the results repo) in inputs/paper_results.json: writes its file list, then, once that
# is committed and pushed there, its commit (see run/fetch_results.py; ~1 min)
pin-paper-results:
    python3 run/fetch_results.py --pin

# Get every dataset a run needs at its scales, each step only if missing (`just fetch-dataset ldbc=0.1,imdb=sample1pct`;
# default the paper's: ldbc=3,linkbench=n1m,imdb=full), into <data dir>/datasets; `--check` verifies what is in place
# against the tracked hashes and downloads nothing (python/kvevaluator/fetch_dataset.py)
fetch-dataset *args:
    {{py}} -m kvevaluator.fetch_dataset {{args}}

# The C++ counts equal the freeze's golden counts at sf0.1 (needs `just fetch-dataset ldbc=0.1`; ~2 min the first time, then seconds)
golden-counts: build
    {{py}} tests/gates/check_golden_counts.py

# A tally change needs a tally version bump: the seven sf0.1 diagonal tallies against the tracked ones (needs `just fetch-dataset ldbc=0.1`; ~10 s)
tally-version: build
    {{py}} tests/gates/check_tally_version.py

# The sf0.1 gates. The round, run/meta-gates.sh, runs gate-prep, cpp-diff and write-gate on one binary; `just gate full`
# runs it with every other check a merge needs.

# Ingest every store write-gate and cpp-diff open that is not there yet (seconds each at sf0.1)
gate-prep sf="0.1":
    {{py}} tests/gates/gate_preflight.py --gate all --sf {{sf}} --prep

# C++ against the Python oracle: every plan file's counts and rows on the sf0.1 stores, run in lanes (run/cpp-diff.sh)
cpp-diff nparam="5" lanes="all":
    run/cpp-diff.sh {{nparam}} {{lanes}}

# The write gate (run/write-gate.sh): write plans, the write suite on the oracle, write effects, the write-count oracle
write-gate sf="0.1" lanes="all":
    run/write-gate.sh {{sf}} {{lanes}}

# Every legal body of every read query gives one answer per (query, row) on every sf0.1 store (ordered where the query
# orders); ~6.5 min (tests/gates/check_bodies_agree.py)
bodies-agree *args:
    {{py}} tests/gates/check_bodies_agree.py {{args}}


# Measure and analyse one paper figure's data: `just results uniform-49 --dataset ldbc=0.1` (no figure: list them and their flags; run/reproduce.py)
results *args:
    {{py}} run/reproduce.py results {{args}}

# Draw one paper item from a `just results` run (`just figure uniform-49 --dataset ldbc=0.1`) or the paper's runs (`--from paper-results`);
# an archive-only item (`just figure datasets`) from paper-results alone. No item: list them (run/reproduce.py)
figure *args:
    {{py}} run/reproduce.py figure {{args}}

# Every figure in order, each step timed, then the archive-only items: `--dataset`, `--jobs N`, `--only`, `--skip`, `--dry-run` (run/reproduce-all.sh)
reproduce-all *args:
    run/reproduce-all.sh {{args}}

# Every derive script on paper-results (no argument: their tables and number keys into <output>/paper/build/), with
# the ran and failed counts and the derives no item names (~3 min; run/reproduce.py)
derive-all:
    {{py}} run/reproduce.py derive-all

# The recorded run times of the paper's runs, read from paper-results/ (needs `just fetch-results`; ~10 s)
run-costs-check:
    {{py}} tests/gates/check_run_costs_paper_results.py
