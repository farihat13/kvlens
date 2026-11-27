#!/usr/bin/env python3
"""Write the records probe's edge-case CSV tree (python/kvevaluator/datasets/ldbc/records_probe/CsvBasic-LongDateFormatter/): every file
the LDBC reader opens, with the header of an LDBC tree and a few hand-made rows holding what a large scale has and
sf0.1 may not (README.md beside this file lists each case). The record stream's key at a larger scale holds the dump
of this tree beside sf0.1's (measure/records_key.py, code_part), so an edit to the reader or the writer that changes
how one of these values is dumped moves every larger stream's key.

    uv run --locked python python/kvevaluator/datasets/ldbc/records_probe/make_records_probe.py [--headers <an LDBC CsvBasic-LongDateFormatter tree>]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "CsvBasic-LongDateFormatter")
MAX_ID = (1 << 59) - 1            # the largest raw id under the vid budget (util/vid.h kVidMaxId = 2^59)
INT64_MAX, INT64_MIN_1 = (1 << 63) - 1, -(1 << 63) + 1

# a value with every escape the JSON writer knows, other control characters, and the characters around them
ESCAPES = "tab\there, quote\" backslash\\ slash/ bs\b ff\f ctrl\x01\x1f del\x7f"   # no \r: the frozen
# Python parser (csv.reader) reads one as a line break, and LDBC has none (sf0.1, sf3)
UNICODE = "2-byte é ñ ü, 3-byte 名前   ﻿, 4-byte 😀 𝔘"

ROWS = {
    # vertices: id first; an empty field is an absent prop
    "dynamic/person_0_0.csv": [
        ["1", "Ann", "Lee", "female", "-315619200000", "1266161530447", "1.2.3.4", "Firefox"],   # a birthday before 1970
        ["2", "  Spaced", "trailing  ", "male", "0", "0", "::1", "Chrome"],                       # padding, zeros
        [str(MAX_ID), "Max", "Id", "male", str(INT64_MAX), str(INT64_MIN_1), "255.255.255.255", "Opera"],
        ["4", "Émile", "Ñúñez", "female", "1", "1", "10.0.0.1", "Safari"],
        ["5", "名前", "😀𝔘", "", "", "2", "", ""],                                                 # empty optional fields
        ["6", ESCAPES, UNICODE, "male", "3", "3", "1.1.1.1", "Internet Explorer"],
    ],
    "dynamic/post_0_0.csv": [
        ["10", "", "1313561140595", "49.246.218.237", "Firefox", "uz", "About X, t \tIn 1987, he reached", "33"],
        ["11", "photo11.jpg", "1313561140596", "1.2.3.4", "Chrome", "", "", "0"],               # a photo: no content
        [str(MAX_ID), "", "1", "1.2.3.4", "Chrome", "tk", ESCAPES, "71"],
        ["13", "", "2", "1.2.3.4", "Chrome", "zh", UNICODE, "40"],
        ["14", "", "3", "1.2.3.4", "Chrome", "en", "  leading and trailing spaces  ", "31"],
    ],
    "dynamic/comment_0_0.csv": [
        ["20", "1313591219961", "46.16.217.105", "Chrome", "yes", "3"],
        ["21", "1313591219962", "46.16.217.105", "Chrome", "a\ttab\tor\ttwo\t", "15"],
        [str(MAX_ID), "4", "1.2.3.4", "Firefox", ESCAPES, "71"],
        ["23", "5", "1.2.3.4", "Firefox", "", "0"],
    ],
    "dynamic/forum_0_0.csv": [["30", "Wall of Ann Lee", "1266161540447"], ["31", UNICODE, "-1"],
                              [str(MAX_ID), ESCAPES, "0"]],
    "static/organisation_0_0.csv": [["40", "company", "Kam_Air", "http://dbpedia.org/resource/Kam_Air"],
                                    ["41", "university", "Université_de_Montréal", ""]],
    "static/place_0_0.csv": [["50", "India", "http://dbpedia.org/resource/India", "country"],
                             ["51", "São_Paulo", "", "city"], ["0", "Zero", "http://x/0", "continent"]],
    "static/tag_0_0.csv": [["60", "Hamid_Karzai", "http://dbpedia.org/resource/Hamid_Karzai"],
                           ["61", "Ölüdeniz_😀", ""], [str(MAX_ID), ESCAPES, "http://x/max"]],
    "static/tagclass_0_0.csv": [["70", "OfficeHolder", "http://dbpedia.org/ontology/OfficeHolder"],
                                ["0", "Thing", "http://www.w3.org/2002/07/owl#Thing"]],
    # person's multi-valued props: several values, one value, a person with only languages, an unknown person
    "dynamic/person_email_emailaddress_0_0.csv": [["1", "ann@a.com"], ["1", "ann@b.com"], ["1", "ann.lee@c.org"],
                                                  ["4", "émile@x.fr"], ["999", "nobody@x.com"], ["6", ESCAPES]],
    "dynamic/person_speaks_language_0_0.csv": [["1", "en"], ["1", "fr"], ["5", "zh"], ["2", ""]],
    # edges: src id, dst id, then the edge's props
    "dynamic/person_knows_person_0_0.csv": [["1", "2", "1271939457947"], ["2", str(MAX_ID), "-5"],
                                            [str(MAX_ID), "1", "0"]],
    "dynamic/person_likes_post_0_0.csv": [["1", "10", "1"], ["2", str(MAX_ID), ""]],
    "dynamic/person_likes_comment_0_0.csv": [["1", "20", "2"]],
    "dynamic/person_hasInterest_tag_0_0.csv": [["1", "60"], [str(MAX_ID), "61"]],
    "dynamic/person_isLocatedIn_place_0_0.csv": [["1", "51"]],
    "dynamic/person_studyAt_organisation_0_0.csv": [["1", "41", "2011"], ["2", "41", ""]],   # no classYear
    "dynamic/person_workAt_organisation_0_0.csv": [["1", "40", "2015"], ["4", "40", "-1"]],
    "dynamic/post_hasCreator_person_0_0.csv": [["10", "1"], [str(MAX_ID), str(MAX_ID)]],
    "dynamic/comment_hasCreator_person_0_0.csv": [["20", "2"]],
    "dynamic/post_hasTag_tag_0_0.csv": [["10", "60"], ["13", "61"]],
    "dynamic/comment_hasTag_tag_0_0.csv": [["21", "60"]],
    "dynamic/post_isLocatedIn_place_0_0.csv": [["10", "50"]],
    "dynamic/comment_isLocatedIn_place_0_0.csv": [["20", "0"]],
    "dynamic/forum_containerOf_post_0_0.csv": [["30", "10"], ["31", str(MAX_ID)]],
    "dynamic/forum_hasMember_person_0_0.csv": [["30", "1", "1266161540447"], ["30", "2", ""]],   # no joinDate
    "dynamic/forum_hasModerator_person_0_0.csv": [["30", "1"]],
    "dynamic/forum_hasTag_tag_0_0.csv": [["31", "61"]],
    "dynamic/comment_replyOf_post_0_0.csv": [["20", "10"]],
    "dynamic/comment_replyOf_comment_0_0.csv": [["21", "20"], [str(MAX_ID), "21"]],
    "static/organisation_isLocatedIn_place_0_0.csv": [["40", "50"]],
    "static/place_isPartOf_place_0_0.csv": [["51", "50"], ["50", "0"]],
    "static/tag_hasType_tagclass_0_0.csv": [["60", "70"], ["61", "0"]],
    "static/tagclass_isSubclassOf_tagclass_0_0.csv": [["70", "0"]],
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headers", help="an LDBC CsvBasic-LongDateFormatter tree to take each file's header from "
                                      "(default: sf0.1's)")
    a = ap.parse_args()
    src = a.headers
    if src is None:
        from kvevaluator.paths import ldbc_dir
        src = os.path.join(str(ldbc_dir("0.1")), "CsvBasic-LongDateFormatter")
    for rel, rows in sorted(ROWS.items()):
        with open(os.path.join(src, rel), encoding="utf-8") as f:
            header = f.readline().rstrip("\n")
        width = header.count("|") + 1
        for r in rows:
            if len(r) != width or any("|" in v or "\n" in v or v.startswith('"') for v in r):
                raise SystemExit(f"make_records_probe: {rel}: a row the reader cannot take as written: {r!r}")
        os.makedirs(os.path.join(OUT, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(OUT, rel), "w", encoding="utf-8", newline="") as f:
            f.write(header + "\n")
            for r in rows:
                f.write("|".join(r) + "\n")
    print(f"make_records_probe: {len(ROWS)} files under {os.path.relpath(OUT)} (headers from {src})")


if __name__ == "__main__":
    main()
