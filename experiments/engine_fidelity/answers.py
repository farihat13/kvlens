"""How two answers are compared: key normalisation and row checks.

Column names differ between the sim's plans and the engine answer parsers (kvengines.ldbc.parsers; camelCase vs snake_case,
entity-prefixed vs bare, singular vs plural), so a like-for-like row comparison needs to fold
both sides onto one key space first (`_norm_key`/`_norm_row`/`_norm_val`). `_rows_match` and
`_live_match` are the two row checks built on top of that: sim vs the optimal plan, and sim vs
the real engine's returned rows. `validate.py` and `deviations.py` both import from here.
"""
from __future__ import annotations


def _norm_val(v):
    """A comparable form of one field. A list of 1-tuples (a sim-side grouped column) reads as the
    list of its elements; a record (a parser's dataclass dict, e.g. IC1's Organization {name, year,
    place}, or the sim's (name, year, place) tuple) reads as the tuple of its values. Every list-valued
    LDBC column (emails, languages, tag names, universities, companies) is a set, and parsers and plans
    order it differently (the parser sorts organisations year-desc), so lists compare sorted."""
    if isinstance(v, dict):
        return tuple(_norm_val(x) for x in v.values())
    if isinstance(v, tuple):
        return v[0] if len(v) == 1 else tuple(_norm_val(x) for x in v)
    if isinstance(v, list):
        # a record inside a list-valued column is a tuple in a Python row and a JSON array in a C++ evaluator row
        # (JSON has no tuple), so an inner list is read as the record it is, never sorted as a set
        return sorted((_norm_val(tuple(x) if isinstance(x, list) else x) for x in v), key=str)
    return v


def _rows_match(sim_rows, opt_rows, ignore=()):
    """Do the engine plan and the optimal plan return the same answer?

    They are two different access paths to one LDBC result, so this is a real correctness
    check -- and the only thing that catches a plan posting a good KV ratio while computing
    the wrong rows (which happened: dgraph IC2 sliced its top-k in traversal order).

    Comparing is fiddly for two reasons, both learned the hard way:
      - the plans carry DIFFERENT field sets. The optimal plans project down to the LDBC
        output columns and drop identity entirely (IS4's optimal row has no `id`, no `_vid`),
        while engine plans keep bookkeeping fields. So compare on the INTERSECTION of the two
        row shapes, not on either one alone.
      - order matters where the query sorts, so compare as sequences, not sets.
    `ignore` drops normalised column names from the comparison (an exemption that allows one
    field to differ still checks every other field of every row).
    """
    if len(sim_rows) != len(opt_rows):
        return "NO"
    if not sim_rows:
        return "YES"
    if not isinstance(sim_rows[0], dict) or not isinstance(opt_rows[0], dict):
        # a vid-list answer (the microbench's nhop / closure finals): a SET of vertices, so
        # compare sorted -- two access paths may emit the same vertices in different orders
        return "YES" if sorted(map(str, sim_rows)) == sorted(map(str, opt_rows)) else "NO"
    sim = [_norm_row(r) for r in sim_rows]
    opt = [_norm_row(r) for r in opt_rows]
    keys = sorted((set(sim[0]) & set(opt[0])) - set(ignore))
    if not keys:
        return "n/a"                       # nothing comparable -- do not claim a pass
    a = [tuple(str(r.get(k)) for k in keys) for r in sim]
    b = [tuple(str(r.get(k)) for k in keys) for r in opt]
    return "YES" if a == b else "NO"


# The optimal plans emit LDBC's output column names (`messageId`, `friendFirstName`) while the
# engine plans emit the underlying predicate names (`id`, `firstName`). Stripping the entity
# prefix and case-folding makes the two comparable; without it the check finds no shared
# columns and silently reports "n/a" -- a pass that proves nothing.
_ALIAS_PREFIXES = ("message", "friend", "author", "person", "post", "comment", "forum",
                   "moderator", "original", "uni", "company", "city", "tag", "creator")


# Two spellings of one column: singular/plural lists (the sim's `email` is the parser's `emails`),
# and IC7's latency (the sim's `latencyMinutes` is the parser's `minutes_latency`) -- without it the
# latency column silently dropped out of every IC7 live check.
_KEY_SYNONYMS = {"email": "emails", "language": "languages", "minuteslatency": "latencyminutes",
                 "organizationworkfrom": "workfrom"}
# Names that START with an entity prefix but aren't prefixed: stripping would turn the sim's
# `cityName` into `name` and `universities` into `versities`, while the parser's `friend_city_name` /
# `friend_universities` strip to `cityname` / `universities` -- and the columns silently fell out of
# the comparison.
_NO_STRIP = {"cityname", "universities", "friendshipcreationdate"}


def _norm_key(k):
    # the parsers' dataclasses use snake_case (`message_creation_date`) where the sim rows
    # use camelCase (`messageCreationDate`); folding out underscores makes them one key.
    low = k.lower().replace("_", "")
    if low not in _NO_STRIP:
        for pre in _ALIAS_PREFIXES:
            if low.startswith(pre) and len(low) > len(pre):
                low = low[len(pre):]
                break
    return _KEY_SYNONYMS.get(low, low)


def _norm_row(r):
    """Row keyed by normalised column name, dropping bookkeeping fields. When two source columns
    normalise to one name (personId and messageId both -> `id`), both keep their full lowercased
    names instead: dropping them would silently take every id out of the comparison."""
    items = [(k, v) for k, v in r.items() if k not in ("_vid", "_tag")]
    names = [_norm_key(k) for k, _ in items]
    clash = {n for n in names if names.count(n) > 1}
    out = {}
    for (k, v), nk in zip(items, names):
        out[k.lower().replace("_", "") if nk in clash else nk] = v
    # LDBC's `messageContent` is `content or imageFile` -- a Post carries imageFile and no
    # content, and the parsers fold the two before returning. Mirror that, or every
    # Post row reads as sim=None vs live='photo...' and looks like a mismatch.
    if out.get("content") in (None, "") and out.get("imagefile"):
        out["content"] = out["imagefile"]
    return out


def _is_placeholder(rows):
    """Is this parser handing back a zero-filled row for an EMPTY engine result?

    The LDBC parsers build a dataclass per result row and, for some queries, emit one
    default-constructed row when the engine returned nothing -- `forum_id=0, forum_title=''`.
    That is a parser artifact, not data: IS6 at reply depth 4-5 measures G=0 on dgraph and
    nebula, so the engine performed no property read at all and cannot have found a forum.

    Without this, a plan that correctly reproduces the engines' 3-hop truncation gets scored
    as returning too FEW rows, which is precisely backwards."""
    return bool(rows) and all(
        all(v in (0, "", None, 0.0, False) for k, v in r.items() if not k.startswith("_"))
        for r in rows)


# Columns where a parser writes a typed default (0 / '') for "no value": IS2's original
# post when the engine's capped reply chase never reaches it. The plan says None; both mean absent.
_PARSER_ABSENT = {"postid": (0, ""), "postauthorid": (0, ""), "postauthorfirstname": ("",),
                  "postauthorlastname": ("",)}


def _same(k, sim_v, live_v):
    if sim_v is None and live_v in _PARSER_ABSENT.get(k, ()):
        return True
    return str(_norm_val(sim_v)) == str(_norm_val(live_v))


# Queries whose LDBC order has a STRING key. LDBC does not fix a collation, and the engines differ
# (ArangoDB compares strings by locale collation, 'Ænema' before 'Albert'; the others by code point).
# So the live check is collation-blind there: (the numeric sort keys before the string key, LIMIT),
# as normalised column names. See _collation_blind.
STRING_SORTS = {
    "ic1": (("distancefromperson",), 20),        # distance asc, lastName asc, id asc
    "ic4": (("count",), 10),                     # postCount desc, tagName asc
    "ic6": (("count",), 10),                     # postCount desc, tagName asc
    "ic11": (("workfrom", "id"), 10),            # workFrom asc, personId asc, organizationName desc
}


def _collation_blind(sim, liv, keys, spec):
    """Do two answers agree whatever string collation ranked them? The numeric sort keys must agree
    at every rank (same blocks, same sizes). Inside a block of equal numeric keys the rows must be the
    same SET (collation only reorders them), except the last block when the LIMIT cuts it: which of its
    rows make the cut is the collation's choice, so only its size is checked. sim / liv: normalised rows."""
    prefix, limit = spec
    pk = lambda r: tuple(str(r.get(k)) for k in prefix)
    if [pk(r) for r in sim] != [pk(r) for r in liv]:
        return False
    row = lambda r: tuple(str(_norm_val(r.get(k))) for k in keys)
    blocks = {}
    for side, rows in (("s", sim), ("l", liv)):
        for r in rows:
            blocks.setdefault(pk(r), {"s": [], "l": []})[side].append(row(r))
    last = pk(sim[-1])
    return all(sorted(b["s"]) == sorted(b["l"]) for p, b in blocks.items()
               if not (p == last and len(sim) >= limit))


def _live_match(sim_rows, live_rows, qname=None):
    """Compare the engine plan's answer to what the REAL engine returned.

    This is the check that matters most: agreeing with our own optimal plan only proves the
    simulator is self-consistent. Returns (verdict, short human diff).

    Differences are not automatically bugs. The live engine is the anchor but not gold: on the
    reply-chase queries it truncates at 3 hops and returns an INCOMPLETE answer (FINDINGS
    13-14), and some LDBC columns are genuinely absent from one side. So a mismatch is
    REPORTED with its shape rather than failed outright."""
    if not sim_rows and not live_rows:
        return "YES", ""
    if not sim_rows and _is_placeholder(live_rows):
        return "YES", f"engine returned nothing ({len(live_rows)} default-filled parser row)"
    if len(sim_rows) != len(live_rows):
        return "NO", f"rowcount sim={len(sim_rows)} live={len(live_rows)}"
    sim = [_norm_row(r) for r in sim_rows]
    liv = [_norm_row(r) for r in live_rows]
    keys = sorted(set(sim[0]) & set(liv[0]))
    if not keys:
        return "n/a", f"no shared columns (sim={sorted(sim[0])[:4]} live={sorted(liv[0])[:4]})"
    bad = []
    for i, (a, b) in enumerate(zip(sim, liv)):
        for k in keys:
            if not _same(k, a.get(k), b.get(k)):
                bad.append(f"row{i}.{k}: sim={str(a.get(k))[:18]!r} live={str(b.get(k))[:18]!r}")
    if not bad:
        return "YES", f"{len(keys)} cols x {len(sim)} rows"
    if qname in STRING_SORTS and _collation_blind(sim, liv, keys, STRING_SORTS[qname]):
        return "YES", f"{len(keys)} cols x {len(sim)} rows; string ties ranked by the engine's collation"
    return "NO", "; ".join(bad[:3]) + (f" (+{len(bad)-3} more)" if len(bad) > 3 else "")
