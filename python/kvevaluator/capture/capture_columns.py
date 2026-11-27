"""The grid capture's columns, one list per file kind (counts, steps, parts; a cell that holds a write request adds the
write lanes): written by grid_bodies (reads), grid_writes (writes) and `kvevaluator predict`, read back through
capture_layout. The lists are inputs/contract/capture_columns.json's file kinds, which the C++ evaluator compiles in
(cpp/kvevaluator/src/cli/capture_writer.cc), so the two sides read one definition."""
from kvevaluator import contract

# Renamed output columns (the glossary's words: a stored key-value pair is a KV pair, never an item; the decode
# counter counts decode requests): old spelling -> new. READ-ONLY: older captures carry the old spellings, so an entry
# is never removed or changed; a new rename is a new entry. Readers apply it (capture_layout.fix_header), the
# step record's shape hashes the old spellings (step_record.columns_sha: a rename alone re-measures nothing), and no
# writer may emit an old spelling (tests/unit/check_capture_layout.py).
RENAMED_COLUMNS = {
    "get_item_hist": "get_kvpair_hist",
    "put_item_hist": "put_kvpair_hist",
    "del_item_hist": "del_kvpair_hist",
    "records_decoded": "decode_requests",
    "reuse_distance_item_blocks": "reuse_distance_kvpair_blocks",
    "n_items": "n_units",
}

# Columns appended to the step record after captures were written without them: column -> (the date it was added, the
# column it follows: a file that holds that one and not this one was written before it).
# READ-ONLY, like RENAMED_COLUMNS. A listed column hashes as absent (step_record.columns_sha), so a capture written
# before it stays fresh; a file without it reads it as "" (not measured, never 0: capture_layout.reader), and an
# accumulating file without it gains it as an empty column (kvstats_layout.aside_other_header). Only for a column whose
# absence moves no other column's value; an unlisted new column still changes the shape (tests/unit/check_capture_layout.py).
APPENDED_COLUMNS = {
    "rt_levels": ("2026-11-09", "range_pages_extra"),   # the round trips level by level (cpp/kvevaluator/src/store/round_trips.h RtText)
    # a write capture's exact write units (store/size_hist.h KvPairHists; steps and parts of a writes capture): an older
    # file's rows read "" and bill_exact refuses them by name
    "put_units_1k": ("2026-11-14", "last_chunk_write_target_bytes"),
    "del_units_1k": ("2026-11-14", "last_chunk_write_target_bytes"),
}


def only_appended_missing(have, want):
    """True when the header `have` is `want` with only appended columns (APPENDED_COLUMNS) left out, or equal to it"""
    return list(have) == [c for c in want if c not in APPENDED_COLUMNS or c in have]

# answer_key: the run's answer as every legal body must give it (candidates.result_key: in the query's order, ties and
# unordered output as sets); result_hash: the rows as returned (raw)
# run_key: the one run's own reuse key (grid_bodies.row_run_key: the body's key, the resolved params, CAPTURE_SHAPE); a
# capture whose key moved reuses a prior row by it (grid_bodies.prior_runs), so the SF30 rule's selection is recomputed
# on every run, and a removed body re-measures nothing
RUN_COLS = contract.columns("counts")
# a cell that holds a write request: RUN_COLS plus the write's own PUTs, DELs and the bytes they write; a read-only cell
# keeps RUN_COLS
WRITE_RUN_COLS = contract.columns("counts_writes")
STEP_COLS = contract.columns("steps")              # the step keys and the shared step record (step_record.columns(""))
# a cell that holds a write request: STEP_COLS plus the last-chunk lane (step_record.LAST_CHUNK); a read-only cell keeps
# STEP_COLS
WRITE_STEP_COLS = contract.columns("steps_writes")
PART_COLS = contract.columns("parts")
WRITE_PART_COLS = contract.columns("parts_writes")
