"""The JOB workload has no parameters: a query's literals are in its text. One execution per query."""


def query_params(tag, qname, count=None):
    return [{}]
