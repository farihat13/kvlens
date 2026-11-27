"""The chunk-target knob of the chunked per-type formats (pertype_csr), one definition for every
Python reader: the format's own pricing (formats/pertype_csr.py) and the C++ store names (measure/stores.py). The default is the
C++ store's (cpp/kvevaluator/src/formats/pertype_csr/format.h: 64K). KVEVAL_PERTYPE_CSR_CHUNK (bytes) selects it everywhere.
"""
import os

PERTYPE_CHUNK_DEFAULT = 65536


def pertype_chunk():
    return int(os.environ.get("KVEVAL_PERTYPE_CSR_CHUNK", str(PERTYPE_CHUNK_DEFAULT)))


# The formats whose stores are built at a chunk target (pertype_chunk above): the one knob the grid drivers, the
# kvstats harness (--chunk exports it), the kv_amp sweep and the Python format's pricing read.
CHUNKED_FORMATS = ("pertype_csr",)
