"""Legacy: what the paper scripts not yet split into a derive script (paper/derive/) and a draw script (paper/draw/)
import. The snapshot names and write_numbers are paper/derive/snapshots.py's, the font is paper/draw/common.py's;
removed when the last of those scripts is split.
"""
from derive.snapshots import *  # noqa: F403   (the snapshot names, write_numbers, NUMBERS_HEADER)
from derive.snapshots import NUMBERS_DIR  # noqa: F401
from kvevaluator.output_tree import Tree
from kvevaluator.paths import ROOT
from draw.common import FONT_FILES, FONTS, SERIF, use_paper_font  # noqa: F401

PAPER = ROOT / "paper"
DRAFT = Tree().paper("draft")            # what the LaTeX draft takes (outputs only)
FIGURES = DRAFT / "figures"
TABLES = DRAFT / "tables"                           # generated tabulars; the draft writes the table, caption and label
