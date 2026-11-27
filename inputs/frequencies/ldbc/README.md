LDBC's own frequency files, one per scale factor: `sf<SF>/driver.properties` is int_driver_v1's
`configuration/ldbc/snb/interactive/sf<SF>.properties`, unchanged. `sf<SF>/mix.json` is I01's input, made from it and
that scale's update stream by `python/kvevaluator/workloads/interactive/gen_ldbc_mix.py --sf <SF>` (only sf3's is made
today; `kvevaluator.datasets.frequencies_in` finds the folder). LDBC's README for these files follows.

---

# Query frequencies

This directory contains the frequencies of the complex read queries prescribed for each scale factor. Audited runs must use these frequencies.

Note that the `.properties` files in this directory are partial ones, i.e. they need to combined with the sample `.properties` files provided in the `driver/` directory of each implementation (e.g. `postgres/driver/benchmark.properties` in LDBC's implementations repository).
