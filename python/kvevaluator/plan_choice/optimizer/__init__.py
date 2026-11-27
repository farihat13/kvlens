"""The plan rule catalog and its helpers -- neutral MECHANISM only.

rules.py: the rule catalog (data) and the expander that turns a logical plan into its candidate physical plans; the
generator (plan_choice/generate.py) writes every candidate into the plan files, and the pick record
(inputs/picks/picks.csv) names the one a store runs. criteria.py: the named pick rules. arms.py: the chunk arms'
capabilities.
Workload knowledge lives with each workload, format knowledge with each format.
"""
