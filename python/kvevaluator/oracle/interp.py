"""Run an IR plan or bundle on the Python oracle's store: bind it (plans/ir/interp.py), then run its steps with
oracle.runner. The sf0.1 checks only."""
from kvevaluator.oracle.runner import run_plan
from kvevaluator.plans.ir.interp import selected_steps


def run_ir(ir, fmt, store, params=None, format_caps=(), verbose=False, caps_of_label=None, scale=None,
           twin_counts=None):
    """Execute an IR plan or bundle. Returns (outputs, kvcosts) exactly like run_plan. Params are
    coerced to their declared types (same as bind_ir), so string param rows work here too -- run_ir
    is the entry a C++/Nebula port mirrors, so it must not need pre-typed params.
    `caps_of_label` makes variant selection per label (bundle.select_variant); `scale` = the store's dataset tag
    (a scale-tagged variant needs it)."""
    steps = selected_steps(ir, params, format_caps, caps_of_label, scale)
    return run_plan(steps, fmt, store, verbose, twin_counts=twin_counts)
