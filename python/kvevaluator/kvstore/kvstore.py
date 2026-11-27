"""The one ordered KV store interface, an abstract in-RAM backend, and the counting
wrapper. This is the qual's KV model in code: GET / SEEK / NEXT for reads, PUT / DELETE
for writes.

Cursor convention (pinned): after seek() the cursor sits AT the first key >= the
target. Reading the current key()/value() is free; next() advances and is the only
thing that counts a NEXT. So reading N elements from a range = 1 SEEK + N NEXT,
and the C++ store counts the same way (no off-by-one).
"""
import bisect
from abc import ABC, abstractmethod

from kvevaluator.cost import KVCost


class Cursor(ABC):
    @abstractmethod
    def valid(self): ...        # is the cursor at a valid position?

    @abstractmethod
    def key(self): ...          # current key (free)

    @abstractmethod
    def value(self): ...        # current value (free, but materializes -> bytes)

    @abstractmethod
    def next(self): ...         # advance one step (counts a NEXT)

    @abstractmethod
    def prev(self): ...         # step backward (counts a NEXT: same iterator move,
                                # opposite direction -- LSM iterators are bidirectional)

    def mark_miss(self):        # the operator declares this landing is NOT what it asked for
        pass                    # (an absence miss); only the counting cursor tallies it


class OrderedKV(ABC):
    @abstractmethod
    def get(self, key): ...     # -> value | None

    @abstractmethod
    def seek(self, key): ...    # -> Cursor at first key >= `key`

    def seek_exact(self, key):
        """The point-lookup idiom: 1 SEEK; the value if the landing IS `key`, else None with
        the landing declared a miss (the counting store puts its foreign key in the miss lane)."""
        cur = self.seek(key)
        if cur.valid() and cur.key() == key:
            return cur.value()
        cur.mark_miss()
        return None

    @abstractmethod
    def seek_for_prev(self, key): ...   # -> Cursor at LAST key <= `key` (reverse walks)

    @abstractmethod
    def put(self, key, value): ...

    @abstractmethod
    def delete(self, key): ...  # remove a key (a no-op if absent)


class AbstractKV(OrderedKV):
    """Deterministic in-RAM store: a dict of values + a lazily-sorted key list. Keys are tuples of comparable parts; a
    tuple sorts before any tuple that extends it, so prefix scans work, and the native order is encode_key's byte order.
    put() is O(1) and invalidates the sort; the order is rebuilt once on the next read. Small-scale only, stdlib only.
    Built for load-then-query: a workload that interleaves writes and reads would re-sort per read.
    """

    def __init__(self):
        self._vals = {}
        self._keys = None        # sorted key list, built lazily

    def put(self, key, value):
        self._vals[key] = value
        self._keys = None        # invalidate the sorted order

    def delete(self, key):
        if self._vals.pop(key, None) is not None:
            self._keys = None    # invalidate the sorted order

    def _sorted_keys(self):
        if self._keys is None:
            self._keys = sorted(self._vals)
        return self._keys

    def get(self, key):
        return self._vals.get(key)

    def seek(self, key):
        keys = self._sorted_keys()
        return _AbstractCursor(keys, self._vals, bisect.bisect_left(keys, key))

    def seek_for_prev(self, key):
        keys = self._sorted_keys()
        return _AbstractCursor(keys, self._vals, bisect.bisect_right(keys, key) - 1)


class _AbstractCursor(Cursor):
    def __init__(self, keys, vals, i):
        self._keys = keys        # snapshot of the sorted key list
        self._vals = vals
        self._i = i

    def valid(self):
        return 0 <= self._i < len(self._keys)

    def key(self):
        return self._keys[self._i]

    def value(self):
        return self._vals[self.key()]

    def next(self):
        self._i += 1

    def prev(self):
        self._i -= 1


class CountingKV(OrderedKV):
    """Wraps any backend and tallies S/G/N + bytes once, backend-agnostic. `sizeof`
    gives a value's byte cost, charged when a value is materialized (a GET, or reading
    a cursor's value()). `keysizeof` gives a key's byte cost (the format's key_bytes,
    a structural + content model), charged in TWO separate columns:
      - `scan_key_bytes`     -- every key a SCAN walks over (the seek's landing key + every
                           next's landing key, once per position). Models iter.bytes.read.
      - `get_key_bytes` -- a point GET's lookup key. Kept SEPARATE from scan_key_bytes because
                           RocksDB's bytes.read for a point Get is value-only (the key
                           lookup is index/bloom work, not key-byte streaming), so
                           total_bytes stays bytes.read-grounded while get_key_bytes gives
                           the key-touch lens (see cost.py).

    Why the scan column is where the ranking lives: a SCAN streams over every key in the
    range (iter.bytes.read counts those key bytes), and for a per-edge-key format the keys
    ARE the payload. So a key-only edge scan pays scan_key_bytes per edge, while a blob read
    pays one key + the value -- the asymmetry that ranks a per-edge-key adjacency above a
    single blob read."""

    def __init__(self, backend, sizeof=lambda v, key=None: 0, keysizeof=lambda k: 0,
                 write_mode=False):
        self._backend = backend
        self._sizeof = sizeof
        self._keysizeof = keysizeof
        # write_mode OFF by default so the BULK LOAD stays uncounted, exactly as before. The
        # load is a one-time cost of building the store; the interesting write number is an
        # INSERT INTO AN EXISTING STORE, which is a different question (see the plan's
        # "Load cost != incremental write cost"). A write workload flips this on for the
        # measured window only; a load-cost study would flip it on during ingest instead.
        self.write_mode = write_mode
        self.cost = KVCost()

    @property
    def backend(self):
        """The raw, UNCOUNTED store underneath. For SETUP/warm reads only -- e.g. a
        format rebuilding a derived in-memory index (arango's vid->collection map) from
        the store when it was opened without ingest. Never use this in a query operator:
        reads through it are invisible to the KVCost tally on purpose."""
        return self._backend

    def reset_counter(self):
        """Zero the KV-op tally (S/G/N + bytes). run_plan calls this before each step
        so every step's cost is measured in isolation."""
        self.cost = KVCost()

    def get(self, key):
        v = self._backend.get(key)
        self.cost.G += 1
        # The lookup key, into its OWN column (never total_bytes). A MISS charges it too --
        # the probe touches a real key regardless of hit (nebula's try-each-tag misses look
        # up an existing key) -- but to the miss lane (cost.py: absence misses).
        if v is not None:
            self.cost.get_key_bytes += self._keysizeof(key)
            self.cost.get_value_bytes += self._sizeof(v, key)  # value only (point GET value)
        else:
            self.cost.G_miss += 1
            self.cost.get_miss_key_bytes += self._keysizeof(key)
        return v

    def seek(self, key):
        self.cost.S += 1
        return _CountingCursor(self._backend.seek(key), self)  # cursor charges its landing key


    def seek_for_prev(self, key):
        self.cost.S += 1
        return _CountingCursor(self._backend.seek_for_prev(key), self)

    def put(self, key, value):
        self._backend.put(key, value)
        if not self.write_mode:
            return                        # bulk load: setup, not a query op -> uncounted
        self.cost.P += 1
        self.cost.write_key_bytes += self._keysizeof(key)
        self.cost.write_value_bytes += self._sizeof(value, key)

    def delete(self, key):
        """Remove a key. A delete writes a KEY and no value, so only write_key_bytes moves --
        which is why nebula/arango's flat 4-DELETE vertex removal is byte-cheap next to
        dgraph's tombstone PUTs over every predicate the vertex touched."""
        self._backend.delete(key)
        if not self.write_mode:
            return
        self.cost.D += 1
        self.cost.write_key_bytes += self._keysizeof(key)


class _CountingCursor(Cursor):
    def __init__(self, inner, owner):
        self._inner = inner
        self._owner = owner
        self._from_seek = True      # this landing came from the seek (else a next/prev)
        self._charge_key()          # the seek landed on a key (position 1) -> read it

    def _charge_key(self):
        """Charge the current key's bytes once per visited position (guarded by
        valid()). Called on construction (seek) and after each next(), so repeated
        key() inspections at the same position don't double-charge."""
        self._value_read = False
        self._missed = False
        if self._inner.valid():
            self._owner.cost.scan_key_bytes += self._owner._keysizeof(self._inner.key())

    def mark_miss(self):
        """The operator declares this landing is NOT what it asked for (a point SEEK that
        landed on a foreign key, an empty prefix, the NEXT past a range's end): the landing
        key moves from scan_key_bytes to the seek/next miss lane and S_miss/N_miss count it.
        Mirrors C++ Cursor::mark_miss; the same guards (twice, or after value())."""
        if self._missed:
            raise RuntimeError("mark_miss() called twice at one landing")
        if self._value_read:
            raise RuntimeError("mark_miss() after value() at the same landing -- a landing whose "
                               "value was read is a hit, not a miss")
        self._missed = True
        n = self._owner._keysizeof(self._inner.key()) if self._inner.valid() else 0
        self._owner.cost.scan_key_bytes -= n
        if self._from_seek:
            self._owner.cost.S_miss += 1
            self._owner.cost.seek_miss_key_bytes += n
        else:
            self._owner.cost.N_miss += 1
            self._owner.cost.next_miss_key_bytes += n

    def valid(self):
        return self._inner.valid()

    def key(self):
        return self._inner.key()    # inspection only -> already charged on landing

    def value(self):
        if self._missed:
            raise RuntimeError("value() at a landing declared a miss -- a miss reads no value")
        self._value_read = True
        v = self._inner.value()
        self._owner.cost.scan_value_bytes += self._owner._sizeof(v, self._inner.key())  # scan value (cursor value())
        return v

    def next(self):
        self._owner.cost.N += 1
        self._inner.next()
        self._from_seek = False
        self._charge_key()          # advanced onto a new key -> read it

    def prev(self):
        # a backward step is the same iterator move as next(), so it counts as a
        # NEXT (physically RocksDB counts it as number.db.prev; we fold both into `next`)
        self._owner.cost.N += 1
        self._inner.prev()
        self._from_seek = False
        self._charge_key()
