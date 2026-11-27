#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/front-by-part-49-layouts-i01/fig_front_mix_vs_uniform_w16.py); edit it there, then run evidence/release_draw.py
"""fig:front-mix-vs-uniform-w16: LDBC Interactive (W16) at SF3, the best mix against the best of the 49 uniform layouts,
panel (a) of fig:front-49 drawn alone by fig_front_by_part_49_layouts_w16.py's panel-(a) form from this bundle's tables.
  figures/evaluation/front-mix-vs-uniform-w16.pdf
    python fig_front_mix_vs_uniform_w16.py --data <dir of the tables> --out <dir>
"""
import common
from common import args
from fig_front_by_part_49_layouts_w16 import main_panel_a

if __name__ == "__main__":
    data, out = args(__doc__)
    main_panel_a(["--panel", "a", "--name", "49-layouts-i01", "--figure", "w16",   # the file name the paper's text reads
                  "--title", f"LDBC Interactive, {common.sf_label()}",
                  "--data", str(data), "--out", str(out)])
