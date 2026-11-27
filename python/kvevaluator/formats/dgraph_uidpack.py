"""Dgraph's UidPack codec for a posting list of uids (dgraph codec.go): the blocks, the group-varint deltas, the
encoded size. DgraphFormat charges a posting's bytes with it when DgraphSizing.pack is on."""


def _varint_width(x):
    """Bytes to hold one intra-block delta (group-varint 2-bit width code -> 1..4 bytes; a
    zero from group padding still costs 1 byte). Real dgraph GUARANTEES every intra-block delta
    is < 2**32 -- a block holds only UIDs sharing the top 32 bits (see _uidpack_blocks) -- so 4
    bytes always suffice; the last branch is a defensive cap correct input never reaches."""
    if x < 0x100:
        return 1
    if x < 0x10000:
        return 2
    if x < 0x1000000:
        return 3
    return 4

_MSB32 = 0xffffffff00000000   # dgraph codec.go: bitMask -- the top 32 bits of a uint64

def _uidpack_blocks(uids, block_size):
    """Split sorted UIDs into blocks exactly as dgraph's Encoder.Add does: close a block on
    EITHER 256 UIDs OR the next UID not sharing the top 32 bits with the block (match32MSB).
    That second cut is what keeps every intra-block delta within uint32 (codec.go casts the
    delta to uint32). For LDBC UIDs (all < 2**32, so top-32-bits all 0) only the size cut fires."""
    blocks, cur = [], [uids[0]]
    for u in uids[1:]:
        if len(cur) >= block_size or (u & _MSB32) != (cur[-1] & _MSB32):
            blocks.append(cur)
            cur = [u]
        else:
            cur.append(u)
    blocks.append(cur)
    return blocks

def uidpack_bytes(uids, block_size=256, ordered=True):
    """Modeled size of Dgraph's UidPack encoding of a UID posting list (codec/codec.go,
    BlockSize=256).
    Sorted-unique UIDs are cut into blocks; each block is Base(8) + NumUids(4),
    then the intra-block deltas, group-varint encoded four at a time (one tag
    byte per group of four, the final partial group PADDED to four -- each padding zero
    costs 1 byte). (A worked example: the list
    [17,23,31,44,300,305] -> 23 bytes).

    `ordered` (default True) = real Dgraph's invariant: postings are stored uid-ASCENDING, so the
    deltas are small and compress. `ordered=False` = the posting is stored in a NON-uid order (a
    date-sorted adjacency, schema.adj_sort/adj_sort_facet): UidPack's ascending-delta
    compression is forfeited, so each uid is modeled at its LOW-32 payload's own varint width
    (no gap coding; the block base carries the top 32 bits -- the C++ codec's rule). That
    is the true price of date-ordering the adjacency."""
    uids = sorted(set(uids)) if ordered else list(uids)  # unordered: keep STORED order
    if not uids:
        return 0
    total = 0
    for blk in _uidpack_blocks(uids, block_size):
        total += 8 + 4                                   # Base + NumUids, once per block
        # ordered: small intra-block deltas. unordered: each uid's LOW-32 payload at its
        # own width (the block base carries the top 32 -- the C++ codec's rule).
        codes = ([blk[j] - blk[j - 1] for j in range(1, len(blk))] if ordered
                 else [u & 0xFFFFFFFF for u in blk])
        for g in range(0, len(codes), 4):
            group = codes[g:g + 4]
            total += 1                                   # the group's tag byte
            total += sum(_varint_width(c) for c in group)
            total += (4 - len(group))                    # padding zeros: 1 byte each
    return total

def _groups_encode(codes, widths):
    """Group-varint bytes: per 4 codes one tag byte (2-bit width-1 per slot, slot 0 in
    the low bits), then each code little-endian at its width. The final partial group is
    padded with width-1 zeros, exactly what uidpack_bytes charges."""
    out = bytearray()
    for g in range(0, len(codes), 4):
        grp, w = list(codes[g:g + 4]), list(widths[g:g + 4])
        grp += [0] * (4 - len(grp))
        w += [1] * (4 - len(w))                          # padding zeros: 1 byte each
        tag = 0
        for s, wid in enumerate(w):
            tag |= (wid - 1) << (2 * s)
        out.append(tag)
        for c, wid in zip(grp, w):
            out += c.to_bytes(wid, "little")
    return bytes(out)

def _groups_decode(b, i, n):
    """Read `n` codes back (the final group's padding is consumed and discarded)."""
    codes = []
    while len(codes) < n:
        tag = b[i]
        i += 1
        for s in range(4):
            wid = ((tag >> (2 * s)) & 3) + 1
            codes.append(int.from_bytes(b[i:i + wid], "little"))
            i += wid
    return codes[:n], i

def uidpack_encode(uids, ordered=True):
    """The REAL bytes of the scheme uidpack_bytes prices -- same blocks, same widths,
    byte-for-byte the modeled size. Per block: Base(8B BE, the first uid) + NumUids(4B BE),
    then group-varint codes. ordered=True (uid-ascending input, asserted by the caller):
    codes = the n-1 intra-block deltas. ordered=False (a date-sorted adjacency):
    codes = each uid's LOW-32 payload at ITS OWN width -- the block base carries the top
    32 bits (the _uidpack_blocks cut), so the payload is all a code stores, and its width
    is the payload's (1-4B), exactly as the C++ codec writes it. (The old full-uid-width
    rule padded every message-uid code to 4B and over-priced dated blobs ~1.2B/entry vs
    the C++ engine's stored bytes -- the dated byte round's gate caught it.)"""
    out = bytearray()
    for blk in _uidpack_blocks(uids, 256):
        out += blk[0].to_bytes(8, "big") + len(blk).to_bytes(4, "big")
        if ordered:
            codes = [blk[j] - blk[j - 1] for j in range(1, len(blk))]
        else:
            codes = [u & 0xFFFFFFFF for u in blk]
        out += _groups_encode(codes, [_varint_width(c) for c in codes])
    return bytes(out)

def uidpack_decode(b, i, n_total=None, ordered=True):
    """Inverse of uidpack_encode from offset `i`: n_total=None reads blocks to the end of
    the buffer (a flat index posting); else exactly n_total uids (an adjacency, whose
    facet array follows). Returns (uids, next offset)."""
    uids = []
    while (i < len(b)) if n_total is None else (len(uids) < n_total):
        base = int.from_bytes(b[i:i + 8], "big")
        n = int.from_bytes(b[i + 8:i + 12], "big")
        i += 12
        if ordered:
            uids.append(base)
            codes, i = _groups_decode(b, i, n - 1)
            cur = base
            for c in codes:
                cur += c
                uids.append(cur)
        else:
            top = base & _MSB32
            codes, i = _groups_decode(b, i, n)
            uids += [top | c for c in codes]   # w<4 forces top==0, so this is exact
    return uids, i
