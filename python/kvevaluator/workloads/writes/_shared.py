"""Parameterization pieces for the writes workload (its own -- the compose_vid/make_vid/mod
FUNCTIONS are infra, but the param/let constants are per-workload, never imported from another
workload). A builder reads its anchor as let("me_vid") (person) or let("src_vid")/let("dst_vid")
(an edge pair), and a freshly inserted synthetic vertex as let("new_vid")."""
from kvevaluator.plans.ir.authoring import param, let, compose_vid, make_vid, mod, add, lit

# Far above every LDBC id (measured <= 46 bits) and below the 59-bit vid ceiling, so a synthetic
# insert can never land on a loaded vertex. Shared by both write sub-workloads (microbench + ldbc).
_SYNTHETIC_BASE = 1 << 50

# The full Person property set an insert carries -- w_ins_v_full (primitives) and IU1 (ldbc ops)
# both use it, so it lives here rather than in either sub-workload.
_FULL_PROPS = {"firstName": "Wtest", "lastName": "Writer", "gender": "other",
               "birthday": 631152000000, "creationDate": 1577836800000,
               "locationIP": "10.0.0.1", "browserUsed": "Chrome",
               "email": ["w@example.org"], "language": ["en"]}


# person is both an anchor vid AND the seed of the synthetic-id arithmetic, so it is an int.
PERSON_PARAM = {"person": {"type": "int"}}
PERSON_LET = {"me_vid": compose_vid("Person", param("person"))}

# an edge pair (iu8 / w_ins_e / w_del_e)
PAIR_PARAM = {"src": {"type": "int"}, "dst": {"type": "int"}}
PAIR_LET = {"src_vid": compose_vid("Person", param("src")),
            "dst_vid": compose_vid("Person", param("dst"))}


def synthetic_vid_let(label, base, salt=0, mod_base=None):
    """let {"new_vid": vid(label, base + salt + person % mod_base)} -- the 'make up a fresh,
    deterministic, non-colliding id from the param' convention (mod_base defaults to base). The
    `%` is composed from existing ops (mod), so no grammar op is added."""
    m = mod(param("person"), lit(mod_base if mod_base is not None else base))
    return {"new_vid": make_vid(label, add(lit(base + salt), m))}
