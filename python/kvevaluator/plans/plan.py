"""query = plan: a plan is an explicit list of Steps; a later step refers to an
earlier step's output with ref(name). No optimizer."""


class Ref:
    def __init__(self, name):
        self.name = name


def ref(name):
    return Ref(name)


class Step:
    def __init__(self, out, op, **args):
        self.out = out      # name of this step's output
        self.op = op        # operator name, e.g. "read_neighbors"
        self.args = args    # operator args; values may be Ref(...)


class Plan:
    """A PARAMETERIZED plan: steps carrying {"param"}/{"let"} holes (as authoring
    Exprs), plus param declarations and let (bind-time derived) definitions. A builder
    that returns a Plan is bound to concrete Steps by kvevaluator.plans.ir.bind(plan, args) before it
    runs, and serializes to one reusable IR file. A builder that still returns a plain
    list of Steps is a baked plan (unmigrated) and runs as before.
    A query someone adds may also declare what its plan file says about it: `question` (the English question) and
    `params_like` (the query whose param rows it runs on, e.g. "ic1"); generate-plans writes both into the plan file.
    A shipped query declares them in its workload's tables instead (questions.json, the manifest), never in both."""
    def __init__(self, steps, params=None, let=None, output=None, question=None, params_like=None, estimate_hints=None):
        self.steps = steps
        self.params = params or {}
        self.let = let or {}
        self.output = output or (steps[-1].out if steps else None)
        self.question = question
        self.params_like = params_like
        self.estimate_hints = estimate_hints   # what predict's cardinality rules may assume (stats.schema.json's contract doc)
