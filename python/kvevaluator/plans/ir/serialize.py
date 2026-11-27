"""Serialize a Python Step-list plan into the language-neutral IR (a JSON-able
dict). This is the compiler front-end: authors keep writing plans in the Step DSL;
this emits the canonical data form.

  Ref("x")        -> {"ref": "x"}
  tuple           -> list
  str/int/.../None-> pass through (op config: etype, prop, k, labels, ...)
  a lambda arg    -> NeedsExpr (filter.pred / derive.fn can't be lifted mechanically;
                     they must be authored as an expression AST -- see expr.py)
"""
from kvevaluator.plans.plan import Ref, Plan
from kvevaluator.plans.ir.authoring import Expr

_EXPR_ARGS = {"pred", "fn", "where", "where_key"}   # args whose value is a callable in the Step DSL
_RESERVED = {"ref", "param", "lit", "let"}   # the value-form keys the interpreter resolves


class NeedsExpr(Exception):
    """Raised when a plan arg is a Python callable, so it can't be serialized to
    data as-is; the query must be re-authored with an expression AST."""


def _val(v, op, key):
    if isinstance(v, Expr):          # an authored expression -> emit its AST (not a lambda)
        return v.ast
    if callable(v):
        why = "a lambda; author it as an expression AST" if key in _EXPR_ARGS else "a callable"
        raise NeedsExpr(f"{op}.{key} is {why}")
    if isinstance(v, Ref):
        return {"ref": v.name}
    if isinstance(v, (list, tuple)):
        return [_val(x, op, key) for x in v]
    if isinstance(v, dict):
        # A data dict shaped like a value-form ({"ref": ...} etc.) would be silently
        # mis-resolved on the way back in. Refuse it loudly rather than corrupt the round
        # trip -- a field/key must not be one of the reserved value-form words.
        if len(v) == 1 and next(iter(v)) in _RESERVED:
            raise NeedsExpr(f"{op}.{key}: data dict key {next(iter(v))!r} collides with a "
                            "value-form keyword; rename the field")
        return {k: _val(x, op, key) for k, x in v.items()}
    return v                                          # str / int / float / bool / None


def step_to_ir(step):
    d = {"name": step.out, "op": step.op}
    for k, v in step.args.items():
        d[k] = _val(v, step.op, k)
    return d


def plan_to_ir(plan, name="plan", params=None):
    """-> an IR dict {plan, params, [let], steps, output}. Accepts a `Plan` (parameterized:
    emits its params + let, so the file has {"param"}/{"let"} holes -- the reusable form) or
    a plain list of Steps (baked: no free params). `let` values may be authoring Exprs
    (their .ast is emitted)."""
    let = None
    if isinstance(plan, Plan):
        steps, params, let, output = plan.steps, plan.params, plan.let, plan.output
    else:
        steps, output = plan, plan[-1].out
    # copy params: builders pass the shared module-level constants (PERSON_PARAM, ...) by
    # reference, so a future per-format stamp writing into ir["params"] must not mutate them.
    d = {"plan": name, "params": dict(params) if params else {}}
    if let:
        d["let"] = {k: (v.ast if hasattr(v, "ast") else v) for k, v in let.items()}
    d["steps"] = [step_to_ir(s) for s in steps]
    d["output"] = output
    return d
