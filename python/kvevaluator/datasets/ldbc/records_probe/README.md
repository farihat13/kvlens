# The records probe: an edge-case LDBC CSV tree

A tiny LDBC CSV tree (33 files, 64 records) whose dump is part of the record stream's freshness key at every scale
above sf0.1 (`python/kvevaluator/measure/records_key.py`, the code part). The key there holds two probes: the bytes
the code dumps for sf0.1, and the bytes it dumps for this tree. sf0.1 alone misses values that only larger scales hold
(SF3's posts have tabs, sf0.1's have none). An edit to the reader or the writer that changes how one of the values
below is dumped moves both SF3's and SF30's keys, even when sf0.1's bytes stay the same.

Run `uv run --locked python python/kvevaluator/datasets/ldbc/records_probe/make_records_probe.py` to write the tree. The script takes each file's header
from sf0.1's CSVs, and the rows are hand-written in the script. To add a case, add a row there and run it again; the
change moves every larger stream's key once.

## What it holds

| case | where | why |
|---|---|---|
| a tab inside a value | posts, comments, `ESCAPES` | SF3 has 17 (post and comment content); sf0.1 has none |
| every JSON escape the writer knows: `"`, `\`, backspace, form feed; other control characters (U+0001, U+001F, DEL) | `ESCAPES` in a Person name, a Post, a Comment, a Forum title, a Tag, an email | an edit to the escaping |
| 2-, 3- and 4-byte UTF-8, U+2028, a byte-order mark | `UNICODE`, names | the writer keeps non-ASCII as it is (`ensure_ascii=False`) |
| leading and trailing spaces, a value of tabs | Person 2, Post 14, Comment 21 | an edit that trims whitespace |
| the largest raw id under the vid budget (2^59 - 1), and id 0 | Person, Post, Comment, Forum and Tag (id 0: Place, TagClass), edges to and from them | the vid encoding at its top |
| int64's largest and next-to-smallest value, negative ints, zero | the largest-id Person, Person 1 and 2, edge props | the int parser and writer |
| empty fields (an absent prop), a photo post with no content | Person 5, Post 11, Comment 23, edge props | the empty-field rule |
| several emails and languages for one person, a person with only languages, an empty language, an email of an unknown person | the two multi-valued files | the multi-valued props |

Not in it: a field that starts with `"` (the reader refuses one: LDBC never quotes), and a `\r` (the frozen Python
parser reads one as a line break; LDBC has none). The C++ reader and the frozen Python parser give this tree the same
bytes (`tests/unit/check_records_key.py`).
