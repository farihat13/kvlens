#!/usr/bin/env python3
"""Generate forum anchors for the wide-read `forum_board_render` query.

LDBC ships no forum params, so this derives them from the data: count each forum's posts (containerOf), drop the
empty forums, and take a rank-SPACED sample across the post-count distribution (big, medium and small boards).
Writes `forum_param.txt` (`forumId|postCount`) into the TRACKED interactive param dir, which params.py reads first.

    python3 python/kvevaluator/datasets/ldbc/gen_forum_params.py --sf 0.1 [--count 15]
"""
import argparse
import csv
import os
from kvevaluator.paths import ldbc_dir
from kvevaluator.datasets import params_in


def container_csv(sf):
    return os.path.join(ldbc_dir(sf), "CsvBasic-LongDateFormatter", "dynamic", "forum_containerOf_post_0_0.csv")


def out_path(sf):
    return os.path.join(params_in("ldbc", sf, "interactive"), "forum_param.txt")


def post_counts(sf):
    """{forum_id: #posts} from the containerOf CSV (pipe-delimited, Forum.id|Post.id)."""
    counts = {}
    with open(container_csv(sf), newline="") as f:
        rd = csv.reader(f, delimiter="|")
        next(rd)                                  # header
        for row in rd:
            counts[row[0]] = counts.get(row[0], 0) + 1
    return counts


def rank_spaced(items, count):
    """`count` items evenly spaced by rank across `items` (already sorted). Endpoints included."""
    if count >= len(items):
        return items
    step = (len(items) - 1) / (count - 1)
    return [items[round(i * step)] for i in range(count)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--count", type=int, default=15)
    a = ap.parse_args()

    ranked = sorted(post_counts(a.sf).items(), key=lambda kv: (-kv[1], int(kv[0])))
    ranked = [(fid, n) for fid, n in ranked if n > 0]
    sample = rank_spaced(ranked, a.count)

    path = out_path(a.sf)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("forumId|postCount\n")
        for fid, n in sample:
            f.write(f"{fid}|{n}\n")
    print(f"wrote {len(sample)} forum anchors -> {path} "
          f"(post counts {sample[0][1]}..{sample[-1][1]}, {len(ranked)} non-empty forums total)")


if __name__ == "__main__":
    main()
