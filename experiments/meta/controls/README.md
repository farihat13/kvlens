A check that has never failed on a known-bad binary is not a gate; these patches make a known-bad binary on demand.

# Known-bad builds: the positive controls of meta's checks

| patch | the bug it reintroduces | the check that must FAIL on it |
|---|---|---|
| `write_effects_empty_list_kept.patch` | a Condensed list (or part) emptied by a write is re-PUT as an empty value instead of DELed. A split list's FIRST part is the one exception the format keeps by design (every id keeps a part to land on); census does not count it | `tests/gates/meta/check_write_effects.py`'s leftover-key check: census on each arena after the writes counts values that hold nothing (`empty_values`); any is a FAIL |
| `write_effects_far_label_zero.patch` | every entry a write adds stores far-end label code 0, whatever the plan's label (`far_of` in writes.cc) | `check_write_effects.py`'s label check: e.g. a hasCreator-in entry stores Comment for a Post |
| `write_effects_dangling_view.patch` | a Condensed vertex delete reads its own list through a temporary view (`lists::View(...).dsts()` in a range-for, a dangling reference), so it skips real neighbours' reverse entries | `check_write_effects.py`: e.g. a knows list still holding a deleted vertex |
| `row_order_condensed_reversed.patch` | a Condensed list handed on in descending neighbour order by `read_neighbors` (the stored order is ascending in every layout). It reverses on both paths: the one-pass append (`run_range_append`) and the two-pass per-source lists | `tests/gates/meta/check_row_order_after_write.py --expect-fail`: the digest of a step's output rows differs between the stores with a Condensed list and the others |
| `store_readahead_off.patch` | the store's iterator readahead turned off (`max_auto_readahead_size = 0` in `table_options`; `inputs/contract/store_config.json` decides RocksDB's default) | `tests/gates/check_store_config.py --expect-fail`: every timed regime's recorded store options show `max_auto_readahead_size = 0`, want 262144 (signature "store option max_auto_readahead_size = 0") |
| `store_fill_cache_off.patch` | the measured reads do not fill the block cache (`fill_cache = false` in `read_options()`, the one ReadOptions of `store/counting.cc`) | `check_store_config.py --expect-fail fill_cache`: every timed regime's recorded store options show `fill_cache = False`, want True (signature "store option fill_cache = False") |
| `far_label_leak_across_runs.patch` | a label remembered outside the plan's dataflow: a cross-run cache in meta that hands a hop's label to any later UNTAGGED value of the vid (labels travel on rows only); keyed by the uid, so a vid and its string spelling both leak | `test_meta`'s far-label cases (a manual control, below): a fresh run's filter on literal vids probes 0 times (want 2), the measured iteration after a warmup that hopped probes 0 (want 2) |

## Building them

`run/meta-build-controls.sh <out dir> [sha]` builds one binary per patch the round uses, named by the patch stem, at the
sha under test (default HEAD), in the build cache: the sha's controls entry (a git archive of the sha) applies each
patch in turn, builds incrementally, keeps a copy and reverses the patch (`run/build_cpp.py --sha <sha> --patch
<file>`); never the working build, and reused when built before. A patch that does not apply, or applies only with
fuzz, fails it (the control would be VOID: re-cut the patch at the current code). So does a control whose binary
equals the unpatched one (the patch did not reach the build), and an entry whose source no longer equals its archive.
Then:

    run/meta-gates.sh --sf 0.1 --controls <out dir>

(`just gate full` does both, with the other checks a merge needs.)

The round runs each write-effects control through `check_write_effects.py --control <binary>`, and the row-order and the
two store-config binaries through their checks. **Caught means its own signature**, the failure text its defect produces
(`check_write_effects.py`'s `CONTROL_SIGNATURES`, by the binary's file name; the row-order binary: "another ORDER" on
every differing step, nothing else). A crash, a throw, or a failure without the signature is CONTROL VOID and fails the
check.

`far_label_leak_across_runs` is a manual control; the round does not run it. Apply it in a scratch checkout, build, and
run `ctest --test-dir <its build dir> -R test_meta --output-on-failure`: caught = the FAILs "far label leak (across
runs)" and "far label leak (across iterations)"; any other failure, a throw or a build error is VOID.

Each patch is tied to the code it was cut against; all seven apply exactly at this tree (`patch -p1 --dry-run` from the
repo root).
