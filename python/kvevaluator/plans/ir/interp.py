"""Bind a language-neutral IR plan/bundle to its params: compile it back into the existing Step objects, so a run of
them (kvevaluator.oracle.interp.run_ir, on the Python store via runner.run_plan) is byte-identical to a hand-written
Python plan (same operators, same cost accounting). IR value forms in a step's args:

  {"ref": step}   -> that step's output (a Ref, resolved by run_plan)
  {"param": name} -> the bound param value
  {"let": name}   -> a bind-time derived value
  {"lit": v}      -> v
  bare scalar/list-> literal op config
  pred / fn args  -> an expression AST with its params / lets bound as literals, as an authoring.Expr: a
                     callable that still carries its AST, so a bound plan serializes back to IR (plan_to_ir)
"""
from kvevaluator.plans.plan import Step, ref as mkref
from kvevaluator.plans.ir.authoring import Expr
from kvevaluator.plans.ir.expr import evaluate
from kvevaluator.plans.ir.bundle import select_variant, expand_fragments, require_bare

_VALUE_KEYS = {"ref", "param", "lit", "let"}
_EXPR_ARGS = {"pred", "fn", "where", "where_key"}   # join_multi's where, where_key: formats/base.py
_PAIR_TEST_ARGS = {"where", "where_key", "where_in", "where_not_in"}   # join_multi only


def _resolve(v, params, lets):
    if isinstance(v, dict) and len(v) == 1 and next(iter(v)) in _VALUE_KEYS:
        (kind, arg), = v.items()
        if kind == "ref":
            return mkref(arg)
        if kind == "param":
            return params[arg]
        if kind == "let":
            return lets[arg]
        return arg                                   # lit
    if isinstance(v, list):
        return [_resolve(x, params, lets) for x in v]
    if isinstance(v, dict):
        return {k: _resolve(x, params, lets) for k, x in v.items()}
    return v


def _bind_ast(ast, params, lets):
    """`ast` with each {"param"} / {"let"} replaced by a {"lit"} of its bound value -- the same evaluation as
    compile_expr(ast, params, lets), and still an AST. A literal is never looked into; a param that is not bound stays
    a reference, so it fails only if evaluated, as before."""
    if isinstance(ast, dict):
        if len(ast) == 1:
            (kind, arg), = ast.items()
            if kind == "lit":
                return ast
            if kind == "param" and arg in params:
                return {"lit": params[arg]}
            if kind == "let" and arg in lets:
                return {"lit": lets[arg]}
        return {k: _bind_ast(x, params, lets) for k, x in ast.items()}
    if isinstance(ast, list):
        return [_bind_ast(x, params, lets) for x in ast]
    return ast


def _eval_lets(let_block, params):
    """Derived params, evaluated once at bind time against NO row. A let may reference
    params and earlier lets (declaration order), not row fields (a field ref KeyErrors)."""
    lets = {}
    for name, ast in (let_block or {}).items():
        lets[name] = evaluate(ast, {}, {"params": params, "lets": dict(lets)})
    return lets


def ir_plan_to_steps(ir_plan, params, lets):
    steps = []
    for s in ir_plan["steps"]:
        args = {}
        for k, v in s.items():
            if k in ("name", "op", "kind"):
                continue
            if k in _PAIR_TEST_ARGS and s["op"] != "join_multi":   # C++ refuses it too (ir/plan.cc prepare_plan)
                raise ValueError(f"step {s['name']!r} ({s['op']}): {k} is join_multi's only")
            args[k] = Expr(_bind_ast(v, params, lets)) if k in _EXPR_ARGS else _resolve(v, params, lets)
        steps.append(Step(s["name"], s["op"], **args))
    return steps


def _flat_plan(ir, format_caps, caps_of_label=None, scale=None):
    if "steps" in ir:
        require_bare(ir, format_caps)            # a bare plan's own store caps (C++ flatten_ir)
    plan = ir if "steps" in ir else dict(
        ir["plans"][select_variant(ir, format_caps, caps_of_label, scale)])
    return expand_fragments(plan, ir.get("fragments", []))


def _coerce(args, decls):
    """Coerce provided args to their declared param types (a declared {"type":"int"} arg that
    arrives as a string -> int). Iterates the ARGS, so an undeclared arg passes through unchanged
    (e.g. a hand-authored IR's `bind` block with no `params` declarations); a DECLARED param that
    is simply absent from args is left out and errors where it is referenced, as before."""
    out = {}
    for k, v in args.items():
        d = decls.get(k, {})
        if d.get("type") == "bytes":              # the JSON form or bytes; a str is refused (C++ ir/plan.cc's rule)
            from kvevaluator.jsonbytes import to_bytes
            out[k] = to_bytes(v)
            continue
        out[k] = int(v) if d.get("type") == "int" and not isinstance(v, int) else v
    return out


def selected_steps(ir, params=None, format_caps=(), caps_of_label=None, scale=None):
    """An IR plan or bundle as runnable steps: its params coerced to their declared types (string rows work), its lets
    evaluated, the variant for `format_caps` / `caps_of_label` (per label) / `scale` selected and its fragments inlined"""
    params = ir.get("bind", {}) if params is None else params
    params = _coerce(params, ir.get("params", {}))
    lets = _eval_lets(ir.get("let"), params)
    return ir_plan_to_steps(_flat_plan(ir, format_caps, caps_of_label, scale), params, lets)


def bind_ir(ir, args):
    """A parameterized IR dict + raw args -> a concrete list of Steps (params/lets resolved,
    types coerced), runnable by runner.run_plan exactly like a baked plan. Split from `bind`
    so a test can round-trip the param'd IR through JSON first (the reusable-template path)."""
    typed = _coerce(args, ir.get("params", {}))
    lets = _eval_lets(ir.get("let"), typed)
    return ir_plan_to_steps(ir, typed, lets)


def bind_body(ir, body, args):
    """One named body of a bundle + raw args -> a concrete list of Steps (bind_ir on that body, the bundle's params,
    lets and fragments): a body no variant selects for the format, run on purpose (a gate's other valid bodies)."""
    typed = _coerce(args, ir.get("params", {}))
    lets = _eval_lets(ir.get("let"), typed)
    return ir_plan_to_steps(expand_fragments(dict(ir["plans"][body]), ir.get("fragments", [])), typed, lets)


def bind(plan, args):
    """A parameterized Plan + raw args -> a concrete list of Steps. The one call that fills the
    holes: serialize the Plan to IR, then bind_ir."""
    from kvevaluator.plans.ir.serialize import plan_to_ir
    return bind_ir(plan_to_ir(plan), args)


