"""Generate the BI param files from the LDBC CSVs -- deterministic (exact percentile picks over sorted
values, no randomness), so re-running on the same data reproduces the files byte for byte.

  $PYTHON -m kvevaluator.workloads.bi.genparams --sf 0.1 [--families <family> ...]

The strata follow the spec's parameter curation (8.3): a query's cost follows a FACTOR (messages before a date,
messages per Tag, ...); a param is picked at a percentile of that factor, so "typical" and "heavy" are named
points, not samples.

Families:
  message_dates   BI1: $datetime at p25 / p50 / p75 / p95 of the message creationDates, then max + 1 ms
                  (every message) -- the factor is the number of messages before the date.
  message_filter  BI12: ($startDate, $lengthThreshold, $languages). The spec's rule: a start date that keeps
                  ~30% of the messages (p70 of the dates), a length threshold at the median length of the messages
                  with content, the most frequent Post languages. Strata: typical = (p70, median length, top 3
                  languages); then (p50, p75 length, top 5), (p85, p25 length, top 2), (p30, p90 length, top 5);
                  heavy = (min - 1, max length + 1, every language) = every message with content.
  date_windows    BI9: [$startDate, $endDate] 90-day windows starting at p20 / p40 / p60 of the message dates, the
                  busiest 100-day window (most messages), and the whole span (heavy).
  tagclass_dates  BI2: ($tagClass, $date): the TagClass at p50 / p75 / p90 / max of messages-per-class (hasType
                  then hasTag counts) with $date = the message-date p30 (the 200-day span fits), then the max
                  class at p50 (heavy).
  tags            BI5 / BI7 / BI8: Tags at p50 / p75 / p90 / p99 / max of messages-per-tag; the three queries
                  take ADJACENT ranks (slots 0 / 1 / 2 below each stratum) so no two share a tag (the spec's
                  "different Tags than Q5 / Q6 / Q7"); BI8 adds a 100-day window at the message-date p40
                  (the heavy row: the busiest 100 days).
  country_pairs   BI14: ($country1, $country2) from the cross-country friendship factor (knows edges whose ends live in
                  different countries): the top pair (correlated, the spec's variant a), p90, p50, p10, and the pair
                  with the fewest (one) friendships (anti-correlated, variant b).
  country_windows BI11: ($country, $startDate, $endDate): the country at max / p90 / p50 of persons-per-country with
                  a 100-day knows-creation window at the p60 / p75 / p85 of the knows dates; then the largest country
                  with the busiest 100 days of friendships, and the largest country over the whole span (heavy).
  tag_days        BI16: ($tagA, $dateA, $tagB, $dateB, $maxKnowsLimit = 4): cell A = the (tag, UTC day) cell at or
                  just below the max / p99 / p90 / p50 / p20 of the cells' message counts (at least 2 messages) that
                  has a partner; cell B = the biggest other cell sharing a QUALIFYING author with A (an author with at
                  most $maxKnowsLimit friends among the cell's authors), so every answer is non-empty.
  person_country_class BI10: ($personId, $country, $tagClass): the person at or just below the max / p90 / p50 / p50 /
                  p10 of the knows degree whose answer for the tagclass_countries row of the same rank (country, class)
                  is non-empty (an expert at knows-distance 3 or 4 living in the country with a message of the class);
                  the stratum says how many ranks below the percentile the pick walked ("deg-p100-37").
  forum_dates     BI4: $date -- the spec says a date in the network's first 30 days (so nearly every Forum is created
                  after it): day 1, day 15 and day 30 after the first Forum's creationDate; then the message-date p75
                  (fewer forums); then the last message date (no Forum after it: the empty answer, on purpose).
  interest_tags   BI18: $tag at p50 / p75 / p90 / p99 / max of interested-persons-per-tag among the tags with a
                  non-empty answer (two interested persons with a mutual friend who do not know each other).
  propagation_tags BI17: $tag at p50 / p75 / p90 / p99 / max of messages-per-tag among the tags with a non-empty
                  answer at delta = 12 h (the query's chain exists in the data).
  tagclass_countries  BI3: ($tagClass, $country): the class at p50 / p90 / max of messages-per-class with the
                  country at p50 / max of persons-per-country (typical = p50 / p50, heavy = max / max).
"""
import argparse
import csv
import gc
import os
from collections import Counter, defaultdict
from itertools import chain

from kvevaluator.datasets.ldbc.loader import _root
from kvevaluator.paths import ROOT
from kvevaluator.workloads.bi.params import FAMILIES, param_path

STRATA = [("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p95", 0.95), ("max", 1.0)]
DAY = 86400000


def _column(sf, fname, col, subdir="dynamic"):
    with open(os.path.join(_root(sf), subdir, fname), newline="") as f:
        rd = csv.reader(f, delimiter="|")
        header = next(rd)
        i = header.index(col)
        return [r[i] for r in rd]


def _write(sf, family, header, rows):
    path = param_path(sf, family)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"                              # a reader during a re-run sees the old file or the new one, never a cut
    with open(tmp, "w") as f:
        f.write("|".join(header) + "\n")
        for r in rows:
            f.write("|".join(str(x) for x in r) + "\n")
    os.replace(tmp, path)
    print(f"  {family}: {len(rows)} rows -> {os.path.relpath(path, ROOT)}")


def message_dates(sf):
    dates = sorted(int(x) for x in _column(sf, "post_0_0.csv", "creationDate") + _column(sf, "comment_0_0.csv", "creationDate"))
    rows = []
    for name, q in STRATA:
        if q >= 1.0:
            rows.append((dates[-1] + 1, len(dates), name))          # every message is before it
        else:
            dt = dates[round(q * (len(dates) - 1))]
            rows.append((dt, sum(1 for d in dates if d < dt), name))
    _write(sf, "message_dates", ["datetime", "messagesBefore", "stratum"], rows)


def message_filter(sf):
    from collections import Counter
    pdates = [int(x) for x in _column(sf, "post_0_0.csv", "creationDate")]
    cdates = [int(x) for x in _column(sf, "comment_0_0.csv", "creationDate")]
    plen = [int(x) for x in _column(sf, "post_0_0.csv", "length")]
    clen = [int(x) for x in _column(sf, "comment_0_0.csv", "length")]
    langs = Counter(x for x, n in zip(_column(sf, "post_0_0.csv", "language"), plen) if n > 0 and x)
    dates = sorted(pdates + cdates)
    lengths = sorted(n for n in plen + clen if n > 0)          # the messages with content
    ranked = [l for l, _ in sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))]
    def at(xs, q):
        return xs[round(q * (len(xs) - 1))]
    rows = [(at(dates, 0.70), at(lengths, 0.50), ";".join(ranked[:3]), "p70-len50-top3"),
            (at(dates, 0.50), at(lengths, 0.75), ";".join(ranked[:5]), "p50-len75-top5"),
            (at(dates, 0.85), at(lengths, 0.25), ";".join(ranked[:2]), "p85-len25-top2"),
            (at(dates, 0.30), at(lengths, 0.90), ";".join(ranked[:5]), "p30-len90-top5"),
            (dates[0] - 1, lengths[-1] + 1, ";".join(ranked), "all")]
    _write(sf, "message_filter", ["startDate", "lengthThreshold", "languages", "stratum"], rows)


def _pairs(sf, fname, subdir="dynamic"):
    """(col0, col1) of an edge CSV, positional (the headers repeat names)."""
    with open(os.path.join(_root(sf), subdir, fname), newline="") as f:
        rd = csv.reader(f, delimiter="|")
        next(rd)
        return [(r[0], r[1]) for r in rd]


def date_windows(sf):
    dates = sorted(int(x) for x in _column(sf, "post_0_0.csv", "creationDate") + _column(sf, "comment_0_0.csv", "creationDate"))
    def at(q):
        return dates[round(q * (len(dates) - 1))]
    rows = [(at(0.20), at(0.20) + 90 * DAY, "p20+90d"), (at(0.40), at(0.40) + 90 * DAY, "p40+90d"),
            (at(0.60), at(0.60) + 90 * DAY, "p60+90d")]
    # the busiest 100-day window: slide over the sorted dates (two pointers)
    best, j = (0, dates[0]), 0
    for i, d in enumerate(dates):
        while dates[j] < d - 100 * DAY + 1:
            j += 1
        if i - j + 1 > best[0]:
            best = (i - j + 1, dates[j])
    rows.append((best[1], best[1] + 100 * DAY, "busiest100d"))
    rows.append((dates[0], dates[-1], "all"))
    _write(sf, "date_windows", ["startDate", "endDate", "stratum"], rows)


def tagclass_dates(sf):
    from collections import Counter
    name_of = dict(zip(_column(sf, "tagclass_0_0.csv", "id", "static"), _column(sf, "tagclass_0_0.csv", "name", "static")))
    class_of = dict(_pairs(sf, "tag_hasType_tagclass_0_0.csv", "static"))          # tag -> class
    per_tag = Counter(t for _, t in _pairs(sf, "post_hasTag_tag_0_0.csv") + _pairs(sf, "comment_hasTag_tag_0_0.csv"))
    per_class = Counter()
    for tag, n in per_tag.items():
        per_class[class_of[tag]] += n
    ranked = sorted(per_class.items(), key=lambda kv: (kv[1], int(kv[0])))      # ascending, ties by id
    dates = sorted(int(x) for x in _column(sf, "post_0_0.csv", "creationDate") + _column(sf, "comment_0_0.csv", "creationDate"))
    d30, d50 = dates[round(0.30 * (len(dates) - 1))], dates[round(0.50 * (len(dates) - 1))]
    def at(q):
        return ranked[round(q * (len(ranked) - 1))]
    rows = [(name_of[at(0.50)[0]], d30, at(0.50)[1], "p50-class"), (name_of[at(0.75)[0]], d30, at(0.75)[1], "p75-class"),
            (name_of[at(0.90)[0]], d30, at(0.90)[1], "p90-class"), (name_of[at(1.0)[0]], d30, at(1.0)[1], "max-class"),
            (name_of[at(1.0)[0]], d50, at(1.0)[1], "max-class-p50date")]
    _write(sf, "tagclass_dates", ["tagClass", "date", "messagesInClass", "stratum"], rows)


def _busiest_window(dates, days):
    best, j = (0, dates[0]), 0
    for i, d in enumerate(dates):
        while dates[j] < d - days * DAY + 1:
            j += 1
        if i - j + 1 > best[0]:
            best = (i - j + 1, dates[j])
    return best[1]


def tags(sf):
    from collections import Counter
    name_of = dict(zip(_column(sf, "tag_0_0.csv", "id", "static"), _column(sf, "tag_0_0.csv", "name", "static")))
    per_tag = Counter(t for _, t in _pairs(sf, "post_hasTag_tag_0_0.csv") + _pairs(sf, "comment_hasTag_tag_0_0.csv"))
    ranked = sorted(per_tag.items(), key=lambda kv: (kv[1], int(kv[0])))      # ascending
    dates = sorted(int(x) for x in _column(sf, "post_0_0.csv", "creationDate") + _column(sf, "comment_0_0.csv", "creationDate"))
    p40 = dates[round(0.40 * (len(dates) - 1))]
    windows = [(p40, p40 + 100 * DAY)] * 4 + [(_busiest_window(dates, 100), _busiest_window(dates, 100) + 100 * DAY)]
    rows = []
    for (name, q), (ws, we) in zip(STRATA, windows):
        base = round(q * (len(ranked) - 1))
        slots = [ranked[max(0, base - k)] for k in (0, 1, 2)]                 # three adjacent ranks, one per query
        rows.append(tuple(x for tag, n in slots for x in (name_of[tag], n)) + (ws, we, name))
    _write(sf, "tags", ["tag5", "messages5", "tag7", "messages7", "tag8", "messages8", "startDate", "endDate", "stratum"], rows)


def tagclass_countries(sf):
    from collections import Counter
    name_of = dict(zip(_column(sf, "tagclass_0_0.csv", "id", "static"), _column(sf, "tagclass_0_0.csv", "name", "static")))
    class_of = dict(_pairs(sf, "tag_hasType_tagclass_0_0.csv", "static"))
    per_tag = Counter(t for _, t in _pairs(sf, "post_hasTag_tag_0_0.csv") + _pairs(sf, "comment_hasTag_tag_0_0.csv"))
    per_class = Counter()
    for tag, n in per_tag.items():
        per_class[class_of[tag]] += n
    classes = sorted(per_class.items(), key=lambda kv: (kv[1], int(kv[0])))
    place = {i: (n, t) for i, n, t in zip(_column(sf, "place_0_0.csv", "id", "static"), _column(sf, "place_0_0.csv", "name", "static"),
                                          _column(sf, "place_0_0.csv", "type", "static"))}
    country_of = dict(_pairs(sf, "place_isPartOf_place_0_0.csv", "static"))         # city -> country
    per_country = Counter(country_of[city] for _, city in _pairs(sf, "person_isLocatedIn_place_0_0.csv"))
    countries = sorted(per_country.items(), key=lambda kv: (kv[1], int(kv[0])))
    def at(xs, q):
        return xs[round(q * (len(xs) - 1))]
    picks = [(0.50, 0.50, "p50-class-p50-country"), (0.90, 0.50, "p90-class-p50-country"), (0.50, 1.0, "p50-class-max-country"),
             (0.90, 1.0, "p90-class-max-country"), (1.0, 1.0, "max-class-max-country")]
    rows = [(name_of[at(classes, qc)[0]], place[at(countries, qp)[0]][0], at(classes, qc)[1], at(countries, qp)[1], name)
            for qc, qp, name in picks]
    _write(sf, "tagclass_countries", ["tagClass", "country", "messagesInClass", "personsInCountry", "stratum"], rows)


def country_pairs(sf):
    from collections import Counter
    place = dict(zip(_column(sf, "place_0_0.csv", "id", "static"), _column(sf, "place_0_0.csv", "name", "static")))
    country_of = dict(_pairs(sf, "place_isPartOf_place_0_0.csv", "static"))
    city_of = dict(_pairs(sf, "person_isLocatedIn_place_0_0.csv"))
    pairs = Counter()
    for a, b in _pairs(sf, "person_knows_person_0_0.csv"):
        ka, kb = country_of.get(city_of[a]), country_of.get(city_of[b])
        if ka and kb and ka != kb:
            pairs[tuple(sorted((ka, kb), key=int))] += 1
    ranked = sorted(pairs.items(), key=lambda kv: (kv[1], int(kv[0][0]), int(kv[0][1])))
    def at(q):
        return ranked[round(q * (len(ranked) - 1))]
    rows = [(place[a], place[b], n, nm) for (a, b), n, nm in
            [(at(1.0)[0], at(1.0)[1], "max-correlated"), (at(0.9)[0], at(0.9)[1], "p90"), (at(0.5)[0], at(0.5)[1], "p50"),
             (at(0.1)[0], at(0.1)[1], "p10"), (at(0.0)[0], at(0.0)[1], "min-anticorrelated")]]
    _write(sf, "country_pairs", ["country1", "country2", "friendships", "stratum"], rows)


def country_windows(sf):
    from collections import Counter
    place = dict(zip(_column(sf, "place_0_0.csv", "id", "static"), _column(sf, "place_0_0.csv", "name", "static")))
    country_of = dict(_pairs(sf, "place_isPartOf_place_0_0.csv", "static"))
    per_country = Counter(country_of[city] for _, city in _pairs(sf, "person_isLocatedIn_place_0_0.csv"))
    countries = sorted(per_country.items(), key=lambda kv: (kv[1], int(kv[0])))
    dates = sorted(int(d) for _, _, d in csv.reader(open(os.path.join(_root(sf), "dynamic", "person_knows_person_0_0.csv"), newline=""), delimiter="|") if d != "creationDate")
    def at(xs, q):
        return xs[round(q * (len(xs) - 1))]
    busiest = _busiest_window(dates, 100)
    rows = [(place[at(countries, 1.0)[0]], at(dates, 0.60), at(dates, 0.60) + 100 * DAY, "max-country-p60+100d"),
            (place[at(countries, 0.9)[0]], at(dates, 0.75), at(dates, 0.75) + 100 * DAY, "p90-country-p75+100d"),
            (place[at(countries, 0.5)[0]], at(dates, 0.85), at(dates, 0.85) + 100 * DAY, "p50-country-p85+100d"),
            (place[at(countries, 1.0)[0]], busiest, busiest + 100 * DAY, "max-country-busiest100d"),
            (place[at(countries, 1.0)[0]], dates[0], dates[-1], "max-country-all")]
    _write(sf, "country_windows", ["country", "startDate", "endDate", "stratum"], rows)


def tag_days(sf):
    from collections import Counter
    DAY = 86_400_000
    cd = {}
    for f in ("post_0_0.csv", "comment_0_0.csv"):
        cd.update(zip(_column(sf, f, "id"), (int(x) for x in _column(sf, f, "creationDate"))))
    name = dict(zip(_column(sf, "tag_0_0.csv", "id", "static"), _column(sf, "tag_0_0.csv", "name", "static")))
    creator = {}
    for f in ("post_hasCreator_person_0_0.csv", "comment_hasCreator_person_0_0.csv"):
        creator.update(_pairs(sf, f))
    LIMIT = 4
    cells, authors = Counter(), {}
    for f in ("post_hasTag_tag_0_0.csv", "comment_hasTag_tag_0_0.csv"):
        for m, t in _pairs(sf, f):
            c = (int(t), cd[m] // DAY)
            cells[c] += 1
            authors.setdefault(c, set()).add(creator[m])
    adj = _knows(sf)
    # the authors a side keeps: at most LIMIT friends among the cell's authors (the query's filter)
    qual, by_author = {}, {}
    for c, ps in authors.items():
        qual[c] = {p for p in ps if len(ps & adj.get(p, set())) <= LIMIT}   # its friends among the cell's authors
        for p in qual[c]:
            by_author.setdefault(p, set()).add(c)
    ranked = sorted(((n, t, d) for (t, d), n in cells.items() if n >= 2))
    def partner(c):
        """The biggest other cell sharing a qualifying author with cell `c` (None when there is none)."""
        cand = {x for p in qual[c] for x in by_author[p] if x != c}
        return max(cand, key=lambda x: (cells[x], x)) if cand else None
    rows = []
    for q, s in ((1.0, "max"), (0.99, "p99"), (0.9, "p90"), (0.5, "p50"), (0.2, "p20")):
        i = round(q * (len(ranked) - 1))
        while partner(ranked[i][1:]) is None:          # walk down to the nearest cell with a partner
            i -= 1
        n, t, d = ranked[i]
        tb, db = partner((t, d))
        rows.append((name[str(t)], d * DAY, name[str(tb)], db * DAY, LIMIT, n, cells[(tb, db)], s + "+partner"))
    _write(sf, "tag_days", ["tagA", "dateA", "tagB", "dateB", "maxKnowsLimit", "countA", "countB", "stratum"], rows)


def person_country_class(sf):
    from collections import Counter
    deg = Counter()
    for x, y in _pairs(sf, "person_knows_person_0_0.csv"):
        deg[x] += 1
        deg[y] += 1
    ranked = sorted(deg.items(), key=lambda kv: (kv[1], int(kv[0])))
    tc = list(csv.DictReader(open(param_path(sf, "tagclass_countries")), delimiter="|"))
    adj = _knows(sf)
    place_id = dict(zip(_column(sf, "place_0_0.csv", "name", "static"), _column(sf, "place_0_0.csv", "id", "static")))
    country_of = dict(_pairs(sf, "place_isPartOf_place_0_0.csv", "static"))
    person_country = {p: country_of[c] for p, c in _pairs(sf, "person_isLocatedIn_place_0_0.csv")}
    class_id = dict(zip(_column(sf, "tagclass_0_0.csv", "name", "static"), _column(sf, "tagclass_0_0.csv", "id", "static")))
    class_of_tag = dict(_pairs(sf, "tag_hasType_tagclass_0_0.csv", "static"))
    creator = {}
    for f in ("post_hasCreator_person_0_0.csv", "comment_hasCreator_person_0_0.csv"):
        creator.update(_pairs(sf, f))
    classes = {}                                                     # person -> the classes of their messages' tags
    for f in ("post_hasTag_tag_0_0.csv", "comment_hasTag_tag_0_0.csv"):
        for m, t in _pairs(sf, f):
            classes.setdefault(creator[m], set()).add(class_of_tag[t])
    def nonempty(p, country, cls):
        dist, frontier = {p: 0}, [p]
        for k in range(1, 5):
            nxt = []
            for x in frontier:
                for y in adj.get(x, ()):
                    if y not in dist:
                        dist[y] = k
                        nxt.append(y)
            frontier = nxt
        return any(k >= 3 and person_country.get(x) == country and cls in classes.get(x, ()) for x, k in dist.items())
    rows = []
    for q, r in zip((1.0, 0.9, 0.5, 0.5, 0.1), tc):
        i = round(q * (len(ranked) - 1))
        while not nonempty(ranked[i][0], place_id[r["country"]], class_id[r["tagClass"]]):   # walk down to a non-empty answer
            i -= 1
        p, d = ranked[i]
        walked = round(q * (len(ranked) - 1)) - i                    # how many ranks below the percentile the pick is
        rows.append((p, r["country"], r["tagClass"], d, f"deg-p{int(q * 100)}" + (f"-{walked}" if walked else "") + f"+{r['stratum']}"))
    _write(sf, "person_country_class", ["personId", "country", "tagClass", "knowsDegree", "stratum"], rows)


def _knows(sf):
    adj = {}
    for x, y in _pairs(sf, "person_knows_person_0_0.csv"):
        adj.setdefault(x, set()).add(y)
        adj.setdefault(y, set()).add(x)
    return adj


def _pick_nonempty(ranked, strata):
    """Rows (factor, key, ...) sorted ascending, all with a non-empty answer: the one at each stratum's percentile."""
    return [(ranked[round(q * (len(ranked) - 1))], s) for s, q in strata]


def forum_dates(sf):
    forums = sorted(int(x) for x in _column(sf, "forum_0_0.csv", "creationDate"))
    msgs = sorted(int(x) for x in _column(sf, "post_0_0.csv", "creationDate") + _column(sf, "comment_0_0.csv", "creationDate"))
    start = forums[0]
    picks = [(start + 1 * DAY, "day1"), (start + 15 * DAY, "day15"), (start + 30 * DAY, "day30"),
             (msgs[round(0.75 * (len(msgs) - 1))], "msg-p75"), (msgs[-1], "msg-max-empty")]
    rows = [(d, sum(1 for f in forums if f > d), s) for d, s in picks]
    _write(sf, "forum_dates", ["date", "forumsAfter", "stratum"], rows)


def interested_pairs(adj, P):
    """{tag: BI18's answer size}: the paths p1-mf-p2 (ordered) with p1 != p2 both interested in the tag and not friends.
    Each person's friends are walked once, not once per tag: per tag, the paths through a middle person mf are
    n * (n - 1), n = mf's friends interested in it; less those whose ends are friends, once per common friend. adj:
    _knows' sets (both directions); P: {tag: its interested persons}. A self-loop would break the count: refused."""
    if any(p in fs for p, fs in adj.items()):
        raise SystemExit("genparams interest_tags: a person knows themself in person_knows_person: the count assumes none")
    tags_of = {}
    for t, ps in P.items():
        for p in ps:
            tags_of.setdefault(p, set()).add(t)
    pairs = defaultdict(int)
    for fs in adj.values():          # the middle person's friends: every path through it, ends in one tag
        for t, n in Counter(chain.from_iterable(tags_of.get(f, ()) for f in fs)).items():
            pairs[t] += n * (n - 1)
    for p1, fs in adj.items():       # less the paths whose ends are friends: per ordered pair, its common friends
        t1 = tags_of.get(p1)
        if not t1:
            continue
        for p2 in fs:
            shared = t1.intersection(tags_of.get(p2, ()))
            if shared:
                common = len(fs & adj[p2])
                for t in shared:
                    pairs[t] -= common
    return pairs


def interest_tags(sf):
    name = dict(zip(_column(sf, "tag_0_0.csv", "id", "static"), _column(sf, "tag_0_0.csv", "name", "static")))
    adj = _knows(sf)
    P = {}
    for p, t in _pairs(sf, "person_hasInterest_tag_0_0.csv"):
        P.setdefault(t, set()).add(p)
    counted = interested_pairs(adj, P)
    ok = []
    for t, ps in P.items():
        if len(ps) < 2:
            continue
        pairs = counted.get(t, 0)
        if pairs:
            ok.append((len(ps), int(t), pairs))
    ok.sort()
    rows = [(name[str(t)], n, pairs, s) for (n, t, pairs), s in _pick_nonempty(ok, [("p50", 0.5), ("p75", 0.75), ("p90", 0.9), ("p99", 0.99), ("max", 1.0)])]
    _write(sf, "interest_tags", ["tag", "interested", "pairs", "stratum"], rows)


def propagation_tags(sf):
    DELTA = 12 * 3600 * 1000                       # the query's delta: 12 h, inside the spec's 8-16 h range
    name = dict(zip(_column(sf, "tag_0_0.csv", "id", "static"), _column(sf, "tag_0_0.csv", "name", "static")))
    cd, creator, parent = {}, {}, {}
    for f in ("post_0_0.csv", "comment_0_0.csv"):
        cd.update(zip(_column(sf, f, "id"), (int(x) for x in _column(sf, f, "creationDate"))))
    for f in ("post_hasCreator_person_0_0.csv", "comment_hasCreator_person_0_0.csv"):
        creator.update(_pairs(sf, f))
    for f in ("comment_replyOf_post_0_0.csv", "comment_replyOf_comment_0_0.csv"):
        parent.update(_pairs(sf, f))
    forum_of_post = {p: f for f, p in _pairs(sf, "forum_containerOf_post_0_0.csv")}
    forum = {}
    def forum_of(m):
        if m not in forum:
            forum[m] = forum_of_post[m] if m not in parent else forum_of(parent[m])
        return forum[m]
    members = set(_pairs(sf, "forum_hasMember_person_0_0.csv"))
    forums_of = {}
    for f, p in members:
        forums_of.setdefault(p, set()).add(f)
    tagged = {}
    for f in ("post_hasTag_tag_0_0.csv", "comment_hasTag_tag_0_0.csv"):
        for m, t in _pairs(sf, f):
            tagged.setdefault(t, set()).add(m)
    ok = []
    for t, M in tagged.items():
        A = {}
        for m1 in M:
            A.setdefault(forum_of(m1), []).append((creator[m1], cd[m1]))
        found = False
        for c in M:
            m2 = parent.get(c)
            if m2 is None or m2 not in M:
                continue
            p2, p3, f2, cd2 = creator[c], creator[m2], forum_of(m2), cd[m2]
            if p2 == p3:
                continue
            for f1 in forums_of.get(p2, set()) & forums_of.get(p3, set()):
                if f1 != f2 and f1 in A and any(cd2 > cd1 + DELTA and (f2, p1) not in members for p1, cd1 in A[f1]):
                    found = True
                    break
            if found:
                break
        if found:
            ok.append((len(M), int(t)))
    ok.sort()
    rows = [(name[str(t)], n, s) for (n, t), s in _pick_nonempty(ok, [("p50", 0.5), ("p75", 0.75), ("p90", 0.9), ("p99", 0.99), ("max", 1.0)])]
    _write(sf, "propagation_tags", ["tag", "messages", "stratum"], rows)


GENERATORS = {"forum_dates": forum_dates, "interest_tags": interest_tags, "propagation_tags": propagation_tags, "message_dates": message_dates, "person_country_class": person_country_class, "tag_days": tag_days, "message_filter": message_filter, "date_windows": date_windows,
              "tagclass_dates": tagclass_dates, "tags": tags, "tagclass_countries": tagclass_countries,
              "country_pairs": country_pairs, "country_windows": country_windows}
INPUTS = {"person_country_class": ["tagclass_countries"]}   # a family that reads another family's file


def run_order(sf, families=None):
    """the families to generate, in params.FAMILIES order (each after the families it reads); refuses a family whose
    input is neither generated in this run nor on disk"""
    wanted = families or FAMILIES
    unknown = sorted(set(wanted) - set(GENERATORS))
    if unknown:
        raise SystemExit(f"genparams: unknown families {unknown} (known: {', '.join(FAMILIES)})")
    order = [f for f in FAMILIES if f in wanted]
    for fam in order:
        missing = [i for i in INPUTS.get(fam, []) if i not in order and not os.path.exists(param_path(sf, i))]
        if missing:
            raise SystemExit(f"genparams: {fam} reads {', '.join(missing)}, which neither this run nor "
                             f"{os.path.dirname(param_path(sf, fam))} has: add it to --families")
    return order


def generate(sf, families=None):
    # Each family runs with the cyclic collector off. A full collection walks every element of the big sets (BI17's
    # forum members and messages per tag: 54M and 112M entries at SF30), and the tuples a family allocates start one
    # every ~90K allocations, so the collector's cost grows about with the square of the scale (BI17 at SF3: 65 s of
    # 127 s; ~2 h at SF30). The families build only dicts, sets, lists and tuples, freed by their reference counts, so
    # turning it off changes no output.
    for fam in run_order(sf, families):
        gc.disable()
        try:
            GENERATORS[fam](sf)
        finally:
            gc.enable()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--families", nargs="*", default=None)
    a = ap.parse_args()
    print(f"BI params for sf{a.sf} from {_root(a.sf)}")
    generate(a.sf, a.families)


if __name__ == "__main__":
    main()
