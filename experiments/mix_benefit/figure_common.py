"""What the mix-benefit figures share: where they read and write, the layouts' order, names and colours, the captions."""
import os
import sys
import textwrap
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.plotting.style import format_color
from kvcost.layout_cost.compose import CELLS  # noqa: F401  (the nine uniform layouts in the grid's order)
from kvcost.layout_cost.labels import SUM_LABEL  # (how every figure names wops_bytes4k)
from kvcost.layout_cost import settings as cells
from kvevaluator.output_tree import Tree

# The working dir of the mix-benefit analyses. A snapshot holds the same parts under <snapshot>/sf3/ (pass that as
# --root).
ROOT = str(Tree().summary("ldbc", "3", "mix-benefit"))

# A layout is coloured by its ADJACENCY setting, with the palette's colour of the format that has that adjacency:
# the three palette keys below (Condensed, Split, Chunked). Its PROPERTIES setting is the hatch.
ADJ = {"condensed": ("Condensed", "all_condensed"), "split": ("Split", "nebula"), "chunked": ("Chunked", "pertype_csr")}
PROPS_HATCH = {"split": "", "condensed": "///", "chunked": "xx"}

MODEL_LABEL = {"wops": "weighted ops", "bytes": "bytes", "wops_bytes4k": SUM_LABEL}
WEIGHTS = "Weighted ops = GET + 1.1 SEEK + 0.01 NEXT on reads; a write adds its own reads, PUTs and DELETEs."
SUM = f"In {SUM_LABEL}, 4 KB of bytes counts as one GET (wops_bytes4k)."
# The caption's default source: the working dir. A figure drawn from a snapshot names it with --source.
SOURCE = "SF3, working output, not snapshotted. Counts are deterministic: no error bars."


def settings(cell):
    """('condensed', 'split') of props_condensed_adj_split."""
    props, adj = cell[len("props_"):].split("_adj_")
    return props, adj


def cell_label(cell):
    """Co-Sp: properties-adjacency (python/kvcost/layout_cost/settings.py)."""
    return cells.name_of(cell)


def adj_color(cell):
    return format_color(ADJ[settings(cell)[1]][1])


def caption(fig, text, width=150):
    """The caption under the figure, wrapped; constrained_layout makes room for it."""
    fig.supxlabel("\n".join(textwrap.wrap(text, width)), fontsize=7.5, x=0.01, ha="left", color="0.25")


def save(fig, out_dir, name, what):
    os.makedirs(out_dir, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out_dir, f"{name}.{ext}"), dpi=150)
    print(f"[{name}] {what} -> {os.path.join(out_dir, name)}.png")
