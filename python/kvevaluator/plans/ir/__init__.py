"""Language-neutral plan IR: author queries as portable data (JSON), run them in
the Python evaluator now, a C++ core / Nebula later.

  serialize.plan_to_ir   Step-list  -> IR dict         (compiler front-end)
  interp.bind_ir         IR dict + args -> the bound plan (run on the oracle: kvevaluator.oracle.interp.run_ir)
  expr                   expression AST -> callable(row)
  bundle                 capability variant select + fragment inlining
"""
from kvevaluator.plans.ir.serialize import plan_to_ir, NeedsExpr        # noqa: F401
from kvevaluator.plans.ir.interp import bind, bind_ir             # noqa: F401
