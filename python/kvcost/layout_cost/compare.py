"""Two cost sets over the same layouts, compared: the one definition of the comparison every reader uses (`kvcost
predict-accuracy`, the layout report kvcost.predict_accuracy.layouts, the plots' rank correlation). One side names a layout (A: e.g.
predicted), the other is the reference it is judged by (B: e.g. measured).

  spearman      Spearman's rank correlation (average ranks on ties; Pearson on the ranks)
  pick_row      A's cheapest layout judged on B: hit, x over B's best, B's rank of it, the runner-up gaps, top-5 overlap,
                Spearman, and the spread (spread)
  spread        B's spread a pick sits in, each x over B's best: the fixed Sp-Co and Co-Co, the median layout, the worst,
                and the gap share = (Sp-Co - pick) / (Sp-Co - best)
  tie_rows      the tie rule: every layout A puts within x of its cheapest, per threshold x, judged on B
  layout_rows   per layout: both totals, A / B, both ranks, B over B's best

The naming side's costs are compared at TIE_DIGITS significant digits (naming_side, weighted_totals): two layouts a
model prices the same tie (then the first layout id in sort order wins), not ordered by summation noise; the judging
side is kept exact. Opens no store.
"""
import math
import statistics

TIE_DIGITS = 12
SP_CO, CO_CO = "props_split_adj_condensed", "props_condensed_adj_condensed"   # the fixed layouts the spread names
THRESHOLDS = (0.0, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30)                       # the tie rule's x


def comparable(x):
    """a cost as it is compared: at TIE_DIGITS significant digits"""
    return float(f"{x:.{TIE_DIGITS}g}")


def naming_side(costs):
    """the side that names a layout (A: predicted), as it is compared: each cost at TIE_DIGITS (comparable), so a tie
    in the model stays a tie; the judging side (B: measured) is kept exact"""
    return {k: comparable(v) for k, v in costs.items()}


def weighted_totals(per_query, layouts, keys, weights, naming):
    """{layout: sum over `keys`, in the order given, of weights[key] x per_query[(layout, key)]}: the naming side's
    totals at TIE_DIGITS (naming_side), the judging side's exact. Every comparison of two sides sums this way, so the
    same costs give the same best and the same ranks wherever they are compared"""
    tot = {c: sum(weights[k] * per_query[(c, k)] for k in keys) for c in layouts}
    return naming_side(tot) if naming else tot


def _ranks(values):
    """average ranks (1-based): ties share the mean of their positions"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Spearman's rank correlation of two equal-length lists: Pearson on the average ranks. NaN when either side is
    constant (or holds fewer than two values)"""
    if len(xs) != len(ys) or len(xs) < 2:
        return float("nan")
    rx, ry = _ranks(list(xs)), _ranks(list(ys))
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if not sxx or not syy:
        return float("nan")
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / math.sqrt(sxx * syy)


def pick_row(name, cur, pred, meas, short):
    """one comparison row from {layout: A's cost} and {layout: B's cost} over the same layouts (A names, B judges);
    `short` gives a layout's short name"""
    cells = sorted(set(pred) & set(meas))
    po = sorted(cells, key=lambda c: (pred[c], c))
    mo = sorted(cells, key=lambda c: (meas[c], c))
    named, best = po[0], mo[0]
    ratio = meas[named] / meas[best] if meas[best] else 1.0
    return {"what": name, "currency": cur, "layouts": len(cells), "named": named, "named_short": short(named),
            "measured_best": best, "measured_best_short": short(best),
            "hit": int(meas[named] <= meas[best] * (1 + 1e-9)),   # a tie with B's best counts
            "named_measured_over_best": ratio,
            "predicted_runner_up_gap": pred[po[1]] / pred[po[0]] - 1 if pred[po[0]] else 0.0,
            "measured_runner_up_gap": meas[mo[1]] / meas[mo[0]] - 1 if meas[mo[0]] else 0.0,
            "measured_rank_of_named": mo.index(named) + 1, "top5_overlap": len(set(po[:5]) & set(mo[:5])),
            "spearman": spearman([pred[c] for c in cells], [meas[c] for c in cells]),
            **spread(named, meas, short)}


def spread(named, meas, short):
    """B's spread a pick sits in, each x over B's best: the fixed Sp-Co and Co-Co, the median layout and the worst;
    and the gap share = (Sp-Co - pick) / (Sp-Co - best), the part of the fixed Sp-Co's excess over the best that the
    pick removes (empty when Sp-Co is the best or is not among the layouts)"""
    best = min(meas.values())

    def over(c):
        return meas[c] / best if c in meas and best else float("nan")
    worst = max(sorted(meas), key=lambda c: meas[c])
    sp = meas.get(SP_CO)
    gap = (sp - meas[named]) / (sp - best) if sp is not None and sp > best else float("nan")
    return {"sp_co_over_best": over(SP_CO), "co_co_over_best": over(CO_CO),
            "median_over_best": statistics.median(meas.values()) / best if best else float("nan"),
            "worst": short(worst), "worst_over_best": over(worst), "gap_share": gap}


def tie_rows(name, cur, pred, meas, thresholds=THRESHOLDS):
    """per threshold x: the layouts A puts within x of its cheapest (k), whether they hold B's best, and the worst and
    best of them on B over B's best"""
    cells = sorted(set(pred) & set(meas))
    pb = min(pred[c] for c in cells)
    mb = min(meas[c] for c in cells)
    best = {c for c in cells if meas[c] == mb}
    out = []
    for x in thresholds:
        within = [c for c in cells if pred[c] <= pb * (1 + x)]
        out.append({"what": name, "currency": cur, "threshold": x, "k": len(within),
                    "contains_measured_best": int(bool(best & set(within))),
                    "worst_measured_over_best_in_set": max(meas[c] for c in within) / mb if mb else 1.0,
                    "best_measured_over_best_in_set": min(meas[c] for c in within) / mb if mb else 1.0})
    return out


def layout_rows(name, cur, a, b, short):
    """per layout both hold, cheapest on B first: A's and B's cost, A / B, A's and B's rank, B over B's best"""
    cells = sorted(set(a) & set(b))
    ra = {c: i + 1 for i, c in enumerate(sorted(cells, key=lambda c: (a[c], c)))}
    rb = {c: i + 1 for i, c in enumerate(sorted(cells, key=lambda c: (b[c], c)))}
    best = min(b[c] for c in cells)
    return [{"what": name, "currency": cur, "layout": c, "name": short(c), "a": a[c], "b": b[c],
             "a_over_b": a[c] / b[c] if b[c] else float("nan"), "a_rank": ra[c], "b_rank": rb[c],
             "b_over_best": b[c] / best if best else float("nan")}
            for c in sorted(cells, key=lambda c: rb[c])]
