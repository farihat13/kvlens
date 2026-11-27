"""The latency layer: the readers of measured latency captures that several studies share (step records per
request, the RocksDB timers, the I01 classes and weights, the step features) and the grid latency driver
(grid_latency.py: it picks the plans; kvevaluator/measure/cell_latency.py times them). It imports kvcost and
kvevaluator only, never a study (tests/lint/check_layers.py)."""
