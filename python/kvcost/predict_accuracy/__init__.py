"""Predict mode's accuracy check: predicted against measured, per step and per layout (`kvcost predict-accuracy`).

join.py joins predicted captures to a measured one; report.py and
write_report.py price the lanes; layouts.py the best of the 49 uniform layouts (python -m kvcost.predict_accuracy.layouts); predicted.py reads a
predicted capture; requests.py writes `kvcost predict`'s requests; uniform.py is `cost-layouts --count-source <a predicted set>`.
"""
