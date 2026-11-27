# LDBC SNB: where the data comes from

`just fetch-dataset ldbc=0.1,ldbc=3` downloads LDBC's own archives into `<data dir>/datasets/ldbc/` (`python -m kvevaluator.paths datasets_dir`
prints the data dir) and checks each one against `archives.json`. A wrong archive or file is refused, and nothing in
place is overwritten. `just fetch-dataset ldbc=0.1 --check` verifies what is there and downloads nothing.

| file | what |
|---|---|
| `archives.json` | per scale factor, LDBC's three archives: the URL, size and sha256 of each, and the size and sha256 of every file inside |
| `expected.json` | per scale factor, the recorded time of a whole fetch (the plan line `just fetch-dataset` prints first) |

Per scale factor, the three archives land at:

| archive | where | who reads it |
|---|---|---|
| CSVs (`CsvBasic-LongDateFormatter`) | `ldbc/sf<SF>/CsvBasic-LongDateFormatter/` | the evaluator's loader, the engines' loaders |
| substitution parameters | `ldbc/substitution_parameters-sf<SF>/` | a fallback only: the IC parameters the queries read are tracked in `inputs/params/ldbc/sf<SF>/interactive/` |
| update streams (numpart-1) | `ldbc/sf<SF>/update_streams/social_network-sf<SF>-numpart-1/` | the live engines' update runs |

Pinned: sf0.1, sf0.3, sf1, sf3 and sf30 (sizes of the archives: 25 MB, 79 MB, 310 MB, 977 MB and 10.7 GB; sf30
extracts to 39 GB). LDBC hosts no substitution parameters for sf0.3 and sf1: `archives.json` marks those
`"hosted": false` and a fetch skips them. The pinned files are byte-identical to the data the paper measured (sf0.1,
sf0.3 and sf3 compared file by file). The host publishes no checksum files; each sf30 archive's
multipart md5 (its S3 ETag, 5 MiB parts) was checked equal to the one the host returns,.

A download that breaks off is tried again, up to three times, from the start (the host does not resume a partial
download). A fetch of SF30 took 12.5 min, each archive in one attempt, and of SF3 60 s. If all three attempts at an
archive fail, the run names it: get the archive by other means into a dir laid out as on the host
(`<dir>/snb-interactive-v1/...`, `<dir>/snb-interactive-v1-updates/...`) and run `python3 python/kvevaluator/datasets/ldbc/fetch.py 30 --host file:///<dir>`;
every archive and file is still checked by sha256.

To add a scale factor: `python3 python/kvevaluator/datasets/ldbc/fetch.py <sf> --pin` into an empty data dir (`KVEVAL_DATA_DIR=<new dir>`). It
downloads the archives, records them in `archives.json` under LDBC's URLs, and refuses an SF already there. With
`--host file:///<dir>` it reads archives already on disk instead, laid out as on the host
(`<dir>/snb-interactive-v1/...`, `<dir>/snb-interactive-v1-updates/...`).

LDBC's query frequencies per scale factor are in `inputs/frequencies/ldbc/`; the records probe (a tiny CSV tree whose
dump keys the record stream above sf0.1) is beside its code, `python/kvevaluator/datasets/ldbc/records_probe/`.
