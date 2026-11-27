"""WriteShadowKV: a copy-on-write overlay, so a write run never changes the store under it.

One base store is shared by every cell of a sweep, so a write cell runs against WriteShadowKV(base): reads merge the
base with an in-RAM overlay, writes land only in the overlay, and the driver drops it after the cell. CountingKV sits
above the shadow (CountingKV(WriteShadowKV(AbstractKV))), so counts are per logical op whichever source served it.
The `rocks` check (our counter == RocksDB's tickers) does not apply to a shadowed cell and is reported n/a;
predicted == actual stays the oracle for write cells.

The merge invariant: a forward cursor (seek, next) keeps the overlay index at or after the base position, a backward
one (seek_for_prev, prev) at or before it. Changing direction mid-walk would break it, so it raises; no write plan
reverses a cursor.
"""
import bisect

from kvevaluator.kvstore.kvstore import Cursor, OrderedKV


class _Tombstone:
    """Marks a key DELETED in the overlay -- distinct from 'absent from the overlay', which
    means 'ask the base'. A plain None would conflate the two and resurrect deleted keys."""
    def __repr__(self):
        return "<deleted>"


_TOMB = _Tombstone()


class WriteShadowKV(OrderedKV):
    def __init__(self, base):
        self._base = base
        self._over = {}          # key -> value, or _TOMB
        self._keys = None        # sorted overlay keys, built lazily (same trick as AbstractKV)

    # --- overlay lifecycle -------------------------------------------------------------
    def reset(self):
        """Drop every buffered write. The driver calls this between cells so each write cell
        starts from the pristine base store -- which is what makes a write sweep repeatable."""
        self._over.clear()
        self._keys = None

    @property
    def pending(self):
        """How many keys the overlay currently shadows (a cheap sanity handle for tests)."""
        return len(self._over)

    def _sorted_keys(self):
        if self._keys is None:
            self._keys = sorted(self._over)
        return self._keys

    # --- OrderedKV ---------------------------------------------------------------------
    def get(self, key):
        if key in self._over:
            v = self._over[key]
            return None if v is _TOMB else v
        return self._base.get(key)

    def put(self, key, value):
        self._over[key] = value
        self._keys = None

    def delete(self, key):
        self._over[key] = _TOMB
        self._keys = None

    def seek(self, key):
        keys = self._sorted_keys()
        return _MergeCursor(self._base.seek(key), keys, self._over,
                            bisect.bisect_left(keys, key), +1)

    def seek_for_prev(self, key):
        keys = self._sorted_keys()
        return _MergeCursor(self._base.seek_for_prev(key), keys, self._over,
                            bisect.bisect_right(keys, key) - 1, -1)


class _MergeCursor(Cursor):
    """A two-way merge of the base cursor and the sorted overlay keys.

    Invariant: `_i` indexes the first overlay key at-or-after the base position when walking
    forward, at-or-before it when walking backward. At a TIE (both sides hold the same key) the
    OVERLAY wins -- it is the newer value -- and stepping advances BOTH sides, so the shadowed
    base copy is never emitted."""

    def __init__(self, base_cur, keys, over, i, step):
        self._b = base_cur
        self._keys = keys
        self._over = over
        self._i = i
        self._step = step       # +1 forward (next), -1 backward (prev)
        self._settle()

    # --- position bookkeeping ---
    def _ovalid(self):
        return 0 <= self._i < len(self._keys)

    def _okey(self):
        return self._keys[self._i]

    def _overlay_leads(self):
        """Is the overlay entry the current head? (Ties count as overlay -- it shadows the base.)"""
        if not self._ovalid():
            return False
        if not self._b.valid():
            return True
        return (self._okey() <= self._b.key()) if self._step > 0 else (self._okey() >= self._b.key())

    def _settle(self):
        """Advance past any head that is a tombstone, so the cursor only ever rests on a key
        that logically exists. A tombstone that shadows a base key consumes the base copy too."""
        while self._ovalid() and self._overlay_leads() and self._over[self._okey()] is _TOMB:
            if self._b.valid() and self._b.key() == self._okey():
                self._step_base()
            self._i += self._step

    def _step_base(self):
        self._b.next() if self._step > 0 else self._b.prev()

    def _advance(self):
        if not self.valid():
            return
        if self._overlay_leads():
            if self._b.valid() and self._b.key() == self._okey():
                self._step_base()          # tie: drop the shadowed base copy with it
            self._i += self._step
        else:
            self._step_base()
        self._settle()

    # --- Cursor ---
    def valid(self):
        return self._b.valid() or self._ovalid()

    def key(self):
        return self._okey() if self._overlay_leads() else self._b.key()

    def value(self):
        return self._over[self._okey()] if self._overlay_leads() else self._b.value()

    def next(self):
        if self._step < 0:
            raise ValueError("WriteShadowKV: cursor from seek_for_prev() walks backward; "
                             "next() would invalidate the merge invariant (see module docstring)")
        self._advance()

    def prev(self):
        if self._step > 0:
            raise ValueError("WriteShadowKV: cursor from seek() walks forward; prev() would "
                             "invalidate the merge invariant (see module docstring)")
        self._advance()
