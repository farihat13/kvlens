"""KVCost: the per-operator cost vector, kept as separate S / G / N + bytes and never collapsed into one scalar.

Read bytes are {get, scan} x {key, value}, named as in the C++ evaluator's Counts:
  - get_key_bytes / get_value_bytes: the key a GET looks up and hits, the value it materializes (a miss adds 0)
  - scan_key_bytes: every key a scan lands on that is what was asked for (the seek's and each next's landing key)
  - scan_value_bytes: the values a cursor materializes during a scan
Absence misses are their own lanes: an op that lands on something it did not ask for (a SEEK onto a foreign key, the
NEXT past a range's end, a GET miss) counts in S_miss / N_miss / G_miss (subsets of S / N / G) and charges the landing
key to seek_miss_key_bytes / next_miss_key_bytes / get_miss_key_bytes; a miss reads no value.

total_bytes (all four read lanes) is the format-comparison currency: the get key is real, format-dependent work, so it
counts. RocksDB's bytes.read excludes the point-Get key, so a comparison against a real engine's counters uses
rocksdb_bytes (get_value + scan_key + scan_value).

Writes are a separate currency, never folded into the read fields (a write's own read-modify-write reads are read
cost). P (PUTs) and D (DELETEs) are kept apart, since "dgraph never issues a real DELETE" is a format finding; W = P + D.
write_key_bytes / write_value_bytes split what a write puts down: op count and byte count can disagree, and both count.
"""
from dataclasses import dataclass


class TwinCountsNotOracle(RuntimeError):
    """a count was read from a Python twin's step whose counts are not its format's (TwinCountsRefused)"""


class TwinCountsRefused:
    """What runner.run_plan returns in place of a step's KVCost when the Python format says its counts for that step
    are not the format's (Format.take_count_caveat). Reading a lane, summing it or comparing it raises
    TwinCountsNotOracle with the reason; a total that includes such a step is refused the same way. The step's ROWS
    are untouched. The named bypass is run_plan's twin_counts argument."""
    __slots__ = ("why",)

    def __init__(self, why):
        object.__setattr__(self, "why", why)

    def _refuse(self, *_a, **_k):
        try:
            why = object.__getattribute__(self, "why")
        except AttributeError:               # a copy or an unpickle in the making: no reason yet
            why = "a refused step's counts (a Python twin's step whose counts are not its format's) cannot be copied"
        raise TwinCountsNotOracle(why)

    def __getattr__(self, name):                 # S, G, N, every byte lane, triple(), copy(), total_bytes, ...
        self._refuse()

    __setattr__ = _refuse
    __add__ = __radd__ = __iadd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = _refuse
    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = _refuse
    __iter__ = __int__ = __float__ = __index__ = __getitem__ = __hash__ = __bool__ = _refuse

    def __repr__(self):
        return f"<REFUSED: {object.__getattribute__(self, 'why')}>"


@dataclass
class KVCost:
    S: int = 0          # SEEKs
    G: int = 0          # GETs
    N: int = 0          # NEXTs
    # READ bytes: the symmetric {get,scan} x {key,value} split (module docstring).
    get_key_bytes: int = 0      # KEY bytes a point GET looks up (hit or miss)
    get_value_bytes: int = 0    # VALUE bytes materialized by a point GET
    scan_key_bytes: int = 0     # KEY bytes a SCAN walks over
    scan_value_bytes: int = 0   # VALUE bytes materialized by a cursor value() during a SCAN
    # --- write currency (see the module docstring) ---
    P: int = 0                    # PUTs
    D: int = 0                    # DELETEs (dgraph: identically 0 -- deletes are tombstone PUTs)
    write_key_bytes: int = 0      # KEY bytes written by a put/delete
    write_value_bytes: int = 0    # VALUE bytes written by a put (a delete writes no value)
    # --- absence misses (module docstring): subsets of S/G/N + the foreign landing key ---
    S_miss: int = 0
    G_miss: int = 0
    N_miss: int = 0
    seek_miss_key_bytes: int = 0
    next_miss_key_bytes: int = 0
    get_miss_key_bytes: int = 0

    def copy(self):
        return KVCost(**self.__dict__)

    @property
    def value_bytes(self):
        """All VALUE bytes materialized: point-GET values + scan values."""
        return self.get_value_bytes + self.scan_value_bytes

    @property
    def key_bytes_read(self):
        """All KEY bytes touched: GET lookup keys + scan landing keys (the key-tax lens),
        hits AND misses -- the touch count does not care whether the key was the one asked for."""
        return self.get_key_bytes + self.scan_key_bytes + self.miss_key_bytes

    @property
    def miss_key_bytes(self):
        """The foreign keys absence misses landed on, all three ops."""
        return self.seek_miss_key_bytes + self.next_miss_key_bytes + self.get_miss_key_bytes

    # "every key touched" per op kind (hit + miss): for a consumer whose stack is
    # value / scan-key / GET-key summing to total_bytes -- the pre-split meaning of the two
    # key columns (measure/cpplatency.whole_key_channels is the CSV-row twin)
    @property
    def scan_key_touched(self):
        return self.scan_key_bytes + self.seek_miss_key_bytes + self.next_miss_key_bytes

    @property
    def get_key_touched(self):
        return self.get_key_bytes + self.get_miss_key_bytes

    @property
    def get_bytes(self):
        """Engine get_bytes analog: point-GET VALUE bytes only. The GET key lookup is
        index/bloom work in a real engine, not a byte-read -- excluded, matching the engines."""
        return self.get_value_bytes

    @property
    def scan_bytes(self):
        """Engine scan_bytes (seek+next) analog: the bytes a scan streams = scan KEY bytes +
        scan VALUE bytes, matching RocksDB iter.bytes.read (key+value per iterated entry).
        A foreign key a seek/next landed on was streamed too, so the miss keys are in."""
        return self.scan_key_bytes + self.seek_miss_key_bytes + self.next_miss_key_bytes + self.scan_value_bytes

    @property
    def total_bytes(self):
        """The format-comparison byte currency: ALL FOUR read channels, get keys included
        (the key tax is real format work -- see the module docstring), misses included (a
        miss touched a real key; only its CLASSIFICATION differs)."""
        return (self.get_key_bytes + self.get_value_bytes + self.scan_key_bytes + self.scan_value_bytes
                + self.miss_key_bytes)

    @property
    def rocksdb_bytes(self):
        """The engine-matching total: what a real RocksDB's bytes.read would count
        (= get_bytes + scan_bytes; the point-Get lookup key excluded). Use this -- never
        total_bytes -- wherever the simulator is compared against real engine counters."""
        return self.get_value_bytes + self.scan_bytes

    @property
    def W(self):
        """All write ops: PUTs + DELETEs. A convenience scalar -- P and D stay separate in the
        vector because the P-vs-D split IS a format property (see the module docstring)."""
        return self.P + self.D

    @property
    def write_bytes(self):
        """All bytes a write puts down: key + value. The write twin of total_bytes."""
        return self.write_key_bytes + self.write_value_bytes

    def triple(self):
        """(S, G, N) — the READ op-count part, what the Stage-0 oracle compares. Byte
        components are measured, not part of the predicted==actual op-count check. Writes have
        their own oracle pair (wpair) so a read-only plan's check is untouched by the write axis."""
        return (self.S, self.G, self.N)

    def wpair(self):
        """(P, D) — the WRITE op-count part, the write oracle's predicted==actual currency.
        Separate from triple() so the two checks report separately: a write plan that gets its
        reads right and its puts wrong should fail on the write line, not silently on a sum."""
        return (self.P, self.D)
