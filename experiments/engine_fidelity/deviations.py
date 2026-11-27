"""Documented engine deviations: which answer differences we excuse, and why.

`EXPECTED_VS_OPTIMAL_DIFF` is an allow-list, not a loosened check: a `rows_match` failure at
any (query, db) NOT in this dict still fails the run (that's how dgraph IC2's top-k-in-
traversal-order bug was first caught). Each entry names a REASON the engine's plan is allowed
to disagree with the optimal plan's rows, and an exemption function that checks the actual rows
still fit that reason -- not just that they differ. Imports the row-comparison primitives it
needs from answers.py.
"""
from __future__ import annotations

from answers import _norm_key, _norm_row, _rows_match

# Cells whose engine plan is EXPECTED to return a different row set from the optimal plan,
# with the reason. This is an allow-list on purpose rather than a loosened check: a NEW
# vs-optimal mismatch anywhere else still fails the run, which is how a real plan bug
# (dgraph IC2 slicing its top-k in traversal order) was caught in the first place.
# Each entry is (reason, check, same_answer). The check sees the actual rows and the param index
# and returns YES / NO, or UNCHECKED where no oracle can say what the engine's deviation should
# return (reported, never counted as a match). same_answer = True only where the deviation leaves
# the LDBC answer unchanged (IC2's client merge) -- the only excused cells the <=-bound may use.
# LIMIT: the row check compares only the columns both plans name alike (`rows_cols`); for some
# queries that leaves out the ids (IC2, IS1/dgraph) -- see the results doc.
def _key_rows(rows):
    return [_norm_row(r) for r in rows]


def _two_branches(sim_rows, opt_rows, pidx):
    """IC2: the engine emits BOTH branches' top-20; the client merges. Merge the sim rows the way
    the client does (creationDate desc, id asc), keep the top len(opt), compare every row."""
    if len(opt_rows) != min(20, len(sim_rows)):     # a branch may hold fewer than 20
        return "NO"
    def msg_id(r):                     # `messageid` once the row also carries `personid` (_norm_row)
        n = _norm_row(r)
        return int(n["messageid"] if "messageid" in n else n["id"])
    merged = sorted(sim_rows, key=lambda r: (-int(_norm_row(r)["creationdate"]), msg_id(r)))
    return "YES" if _rows_match(merged[:len(opt_rows)], opt_rows) == "YES" else "NO"


def _reply_depth(pidx):
    """The message-anchored param files are stratified 6 per reply depth, depths 0-5 (docstring)."""
    return (pidx - 1) // 6


def _capped_chase(sim_rows, opt_rows, pidx):
    """IS6: a comment deeper than the engines' 3-hop cap never reaches its Post -> no row. Only at
    a param whose message is deeper than 3 hops; an empty answer anywhere else is a bug."""
    return "YES" if (not sim_rows and opt_rows and _reply_depth(pidx) > 3) else "NO"


def _five_five(sim_rows, opt_rows, pidx):
    """Nebula IS2: top-5 Posts + top-5 Comments, not the global top-10. Split both answers by
    message type (a Post is its own original post); each sim type-list must agree with the
    optimal's same-type rows on their common prefix (both are that type's newest messages), and
    a person with messages must get rows back. Compares message id + date, not the capped
    originalPost fields."""
    if opt_rows and not sim_rows:
        return "NO"
    def split(rows):
        posts, coms = [], []
        for r in _key_rows(rows):
            (posts if str(r.get("postid")) == str(r.get("id")) else coms).append(
                (str(r.get("id")), str(r.get("creationdate"))))
        return posts, coms
    (sp, sc), (op, oc) = split(sim_rows), split(opt_rows)
    # at most 5 per type, and at least as many as the optimal's top-10 already shows of that type
    # (capped at 5): the person has at least that many, so the engine must return them
    if len(sp) > 5 or len(sc) > 5 or len(sp) < min(5, len(op)) or len(sc) < min(5, len(oc)):
        return "NO"
    ok = all(a[:min(len(a), len(b))] == b[:min(len(a), len(b))] for a, b in ((sp, op), (sc, oc)))
    return "YES" if ok else "NO"


# originalPost* columns after _norm_key (the "original" prefix is stripped)
_ORIGINAL_POST_FIELDS = ("postid", "postauthorid", "postauthorfirstname", "postauthorlastname")


def _capped_chase_field(sim_rows, opt_rows, pidx):
    """Dgraph IS2: same messages in the same order; only the originalPost/author fields of a deep
    comment differ (resolved to a capped ancestor). Every other column of every row must match."""
    return "YES" if _rows_match(sim_rows, opt_rows, ignore=_ORIGINAL_POST_FIELDS) == "YES" else "NO"


def _any_friend_count(sim_rows, opt_rows, pidx):
    """Dgraph IC5: our query counts a forum's posts by ANY friend, where LDBC counts only the members
    who joined that forum after minDate -- a different question, and at sf0.3 the two top-20s share no
    forum, so a row-by-row test against the optimal answer compares nothing. No oracle here can say
    what the engine should return: UNCHECKED. The live answer check is the evidence for this pair."""
    return "UNCHECKED"


def _string_vid_order(sim_rows, opt_rows, pidx):
    """Nebula IC3: `ORDER BY count DESC, personId ASC` where personId is id(other), the STRING vid
    ("person_<id>"), so a count tie sorts as strings where LDBC sorts the numbers. Same rows (every
    column), the same count at every rank; only the order inside a tie may differ."""
    key = lambda r: tuple(sorted((k, str(v)) for k, v in _norm_row(r).items()))
    counts = lambda rows: [int(_norm_row(r)["count"]) for r in rows]
    if len(sim_rows) != len(opt_rows) or counts(sim_rows) != counts(opt_rows):
        return "NO"
    return "YES" if sorted(map(key, sim_rows)) == sorted(map(key, opt_rows)) else "NO"


def _no_birthday_window(sim_rows, opt_rows, pidx):
    """Dgraph IC10: our query applies no birthday window (left to the client, which does not apply it),
    so the engine ranks EVERY friend-of-friend where LDBC ranks only those born in the window -- a
    superset, scored the same way. So every LDBC row that ranks at or above the engine's last row must be
    in the engine's answer, every column equal. UNCHECKED when no LDBC row ranks that high."""
    if not sim_rows:
        return "YES" if not opt_rows else "NO"
    key = lambda r: (-int(_norm_row(r)["commoninterestscore"]), int(_norm_row(r)["id"]))
    cols = lambda r: {k: str(v) for k, v in _norm_row(r).items()}
    cut = key(sim_rows[-1])
    above = [r for r in opt_rows if key(r) <= cut]
    if not above:
        return "UNCHECKED"
    sim = {key(r): cols(r) for r in sim_rows}
    return "YES" if all(sim.get(key(r)) == cols(r) for r in above) else "NO"


def _first_200_likers(sim_rows, opt_rows, pidx):
    """Dgraph IC7: `result(func: uid(L), first: 200)` ranks only the first 200 likers in the live
    Dgraph's uid order (first_k_by_uid, from the captured uid map), where LDBC ranks every liker --
    a different question whenever there are more than 200. The optimal answer is no oracle for it:
    UNCHECKED, not a pass. The live answer check (same 200, by uid) is the evidence."""
    return "UNCHECKED"


EXPECTED_VS_OPTIMAL_DIFF = {
    ("ic2", "dgraph"): ("engine emits BOTH branches' top-20 (40 rows); the CLIENT merges to 20",
                        _two_branches, True),
    ("ic2", "nebula"): ("engine emits BOTH branches' top-20 (40 rows); the CLIENT merges to 20",
                        _two_branches, True),
    ("is6", "dgraph"): ("engine caps the reply chase at 3 hops, so a deeper comment never reaches "
                        "its root Post and NO row is returned", _capped_chase, False),
    ("is6", "nebula"): ("engine caps the reply chase at 3 hops, so a deeper comment never reaches "
                        "its root Post and NO row is returned", _capped_chase, False),
    ("is2", "nebula"): ("nebula IS2 returns top-5 posts + top-5 comments (nGQL cannot ORDER/LIMIT "
                        "after UNION ALL -- the query's declared deviation)", _five_five, False),
    ("ic5", "dgraph"): ("our dgraph query counts a forum's posts by ANY friend, not only the members "
                        "who joined it after minDate (its declared deviation); a different question, "
                        "so no oracle but the live answer", _any_friend_count, False),
    ("ic7", "dgraph"): ("dgraph ranks only the first 200 likers in its uid order (`first: 200`), "
                        "LDBC every liker; the live answer is the check", _first_200_likers, False),
    ("ic3", "nebula"): ("nebula orders personId as its string vid (\"person_<id>\"), so a count tie "
                        "sorts as strings; same rows, same counts", _string_vid_order, False),
    ("ic10", "dgraph"): ("our dgraph query applies no birthday window (the client does not either), so it "
                         "ranks every friend-of-friend; LDBC's rows above its cut must match",
                         _no_birthday_window, False),
    ("is2", "dgraph"): ("dgraph unrolls the replyOf chase a fixed few levels, so a deep comment's "
                        "originalPost resolves to a capped ancestor; every other field matches",
                        _capped_chase_field, False),
}


# The engines' LOADED DATA can differ from the store we build from the CSVs. Such a difference is
# not a plan deviation, so it is not excused above: the live answer is mapped back onto our data by
# an exact rule, and then every column is compared as usual. (reason, fn(sim_rows, live_rows) ->
# live rows).
def _like_dates_10s_early(sim_rows, live_rows):
    """Dgraph IC7: its loader stores every `likes` creationDate exactly 10 s early, to match the
    upstream golden RDF (dgraph/scripts/ldbc/rdf_mappings.py, facet_offset_ms). So the live like date
    + 10 s is ours, and the live latency, floor((like - 10 s - msg) / 1 min), is ours or one less."""
    out = []
    for i, r in enumerate(live_rows):
        r = dict(r)
        sim_lat = _norm_row(sim_rows[i]).get("latencyminutes") if i < len(sim_rows) else None
        for k, v in r.items():
            nk = _norm_key(k)
            if nk == "likecreationdate" and v is not None:
                r[k] = int(v) + 10000
            elif nk == "latencyminutes" and sim_lat is not None and v is not None and int(v) == int(sim_lat) - 1:
                r[k] = sim_lat
        out.append(r)
    return out


LIVE_DATA_DEVIATIONS = {
    ("ic7", "dgraph"): ("the Dgraph loader stores like dates 10 s early (golden-RDF match)",
                        _like_dates_10s_early),
}