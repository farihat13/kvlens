"""The steady regime's rules, one definition for every driver that warms a bounded cache (measure/cpplatency.py's
run_steady): each pass runs every key once in a seeded shuffle of the sorted keys
(the same order on every store, never a fixed adjacency), and the cache is settled when the pass hit rate moves by
less than eps from the pass before. Warm passes take their order by their index; the passes after them (measured, then
the timer passes) by their own index from 0, so measured pass k runs the same order on every store
whatever its warm-up count, and per-pass ratios between stores pair like with like."""
import random


def pass_order(keys, seed, npass):
    """pass `npass`'s order: random.Random(seed + npass) shuffling the sorted keys"""
    order = sorted(keys)
    random.Random(seed + npass).shuffle(order)
    return order


# the passes after the warm-up are seeded apart from the warm passes (seed + MEASURED_SEED_OFFSET + k): never the order
# of a warm pass
MEASURED_SEED_OFFSET = 1000
PASS_ORDER_RULE = "measured-index"     # written into a steady capture's meta


def measured_order(keys, seed, k):
    """the order of the k-th pass after the warm-up (k from 0: the measured passes, then the timer passes)"""
    return pass_order(keys, seed + MEASURED_SEED_OFFSET, k)


def settled(prev_rate, rate, eps):
    """the cache is settled when this pass's hit rate moved less than eps from the pass before"""
    return prev_rate is not None and abs(rate - prev_rate) < eps
