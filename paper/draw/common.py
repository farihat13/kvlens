# released from gs-paper at 62acd0b (evidence/common.py); edit it there, then run evidence/release_draw.py
"""What every draw script shares: its two arguments (--data: the dir of its table; --out: where it writes figures/ and
tables/), reading a table, and the paper's font. A draw script reads only its claim's table (evidence/<claim>/data/ in
gs-paper; build/data/ of the artifact's derive), never a snapshot; it imports nothing from the artifact.
"""
import argparse
import csv
import sys
from pathlib import Path

FONTS = Path(__file__).resolve().parent / "fonts"   # the figures' text font, vendored (fonts/README.md)
SERIF = "Linux Libertine O"                            # the paper's text font (acmart loads libertine)
FONT_FILES = {("normal", "normal"): FONTS / "LinLibertine_R.otf", ("bold", "normal"): FONTS / "LinLibertine_RB.otf",
              ("normal", "italic"): FONTS / "LinLibertine_RI.otf"}       # (weight, style) -> file


SCALE = "3"                                          # the LDBC scale the tables come from (--scale; the paper's: 3)


def args(description):
    """(the table dir, the output dir) from --data and --out; --scale sets SCALE (sf_label)"""
    global SCALE
    ap = argparse.ArgumentParser(description=description.strip().splitlines()[0])
    ap.add_argument("--data", required=True, type=Path, help="the dir holding the claim's table(s)")
    ap.add_argument("--out", required=True, type=Path, help="where to write: <out>/figures/..., <out>/tables/...")
    ap.add_argument("--scale", default=SCALE, help="the LDBC scale the tables come from (default 3: the paper's)")
    a = ap.parse_args()
    SCALE = a.scale
    return a.data, a.out


def sf_label():
    """the LDBC scale as a figure names it: SF3, SF0.1"""
    return f"SF{SCALE}"


def read_table(data, name):
    """the rows of <data>/<name>.csv, as dicts of strings"""
    with open(Path(data) / f"{name}.csv", newline="") as fh:
        return list(csv.DictReader(fh))


def has_table(data, name):
    """whether <data>/<name>.csv is there: a run that did not make a panel's table (a dataset it did not measure) draws
    that panel as empty, named"""
    return (Path(data) / f"{name}.csv").is_file()


def _tex_cell(cell):
    """one tabular cell as plain text: \\cmd[opt]{...}{...}{x} keeps its last argument (\\multicolumn, \\multirow,
    \\makecell, \\emph, \\textbf), its line breaks a space; math and spacing marks as their characters"""
    import re
    for a, b in ((r"$\times$", "×"), (r"$<$", "<"), (r"$>$", ">"), (r"\%", "%"), (r"\,", " "), ("~", " "),
                 (r"\\", " ")):
        cell = cell.replace(a, b)
    cell = cell.replace("{}", "")               # a column format's @{} or a bare {}
    while True:                                 # innermost command first: \cmd[opt]{a}{b}{x} -> x
        m = re.search(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?((\{[^{}]*\})+)(?!\{)", cell)
        if not m:
            break
        last = re.findall(r"\{([^{}]*)\}", m.group(2))[-1]
        cell = cell[:m.start()] + last + cell[m.end():]
    cell = re.sub(r"\$\^\\dagger\$|\$\^\{?\\dagger\}?\$", "†", cell)
    return re.sub(r"[{}$]", "", cell).strip()


def tex_rows_md(lines):
    """a generated tabular's rows (the lines of a .tex the draw scripts write) as a markdown table: one row per line
    with `&`, a \\multicolumn{n} cell followed by n-1 empty ones; rules, comments and the environment dropped. A
    one-cell row (a section heading inside the table) is a bold row"""
    import re
    rows = []
    for line in lines:
        line = re.split(r"(?<!\\)%", line)[0] if not line.lstrip().startswith("\\[") else ""   # a comment, not \%
        line = re.sub(r"\\(toprule|midrule|bottomrule|cmidrule(\([^)]*\))?\{[^}]*\})", "", line).strip()
        if not line.endswith(r"\\") or line.startswith(r"\begin"):
            continue
        cells = []
        for c in line[:-2].split("&"):         # a body row repeats a spanned value (each column has it); a header not
            n = re.match(r"\s*\\multicolumn\{(\d+)\}", c)
            v = _tex_cell(c)
            cells += [v] + [v if rows and len(line[:-2].split("&")) > 1 else ""] * (int(n.group(1)) - 1 if n else 0)
        rows.append(cells)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    for r in rows:
        if r[0] and not any(r[1:]) and width > 2:
            r[0] = f"**{r[0]}**"
    md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    return md + ["| " + " | ".join(r) + " |" for r in rows[1:]]


def write_table(out, lines, title):
    """the tabular `lines` to `out` (.tex), and the same rows to out.with_suffix(".md") under `title`, for a reader"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    out.with_suffix(".md").write_text("\n".join([title, "", *tex_rows_md(lines), ""]))
    print(f"wrote {out} and {out.with_suffix('.md').name}")


def empty_panel(ax, title, why="not in this run", pad=9):
    """a panel whose table the run did not make: its title (pad: as the figure's other titles), the reason in its middle"""
    ax.set_title(title, fontsize=7.5, pad=pad)
    ax.text(0.5, 0.5, why, transform=ax.transAxes, ha="center", va="center", fontsize=6.5, color="#666666")
    ax.set_xticks([])
    ax.set_yticks([])


_FONT_DIR = None                                     # the private dir use_paper_font registers the fonts from


def use_paper_font():
    """Set the figures' text and math letters to the vendored Linux Libertine (other math symbols: STIX); exit when it
    is missing, never fall back."""
    import atexit
    import os
    import shutil
    import tempfile
    from pathlib import Path

    from matplotlib import font_manager, get_data_path
    import matplotlib.pyplot as plt
    missing = [str(p) for p in FONT_FILES.values() if not p.is_file()]
    if missing:
        sys.exit(f"paper font: {', '.join(missing)} not found; the figures are drawn only in {SERIF} from {FONTS} "
                 f"(see its README.md), never in a fallback font")
    # matplotlib's PDF backend embeds a figure's fonts sorted by their file paths, so the PDF bytes would depend on
    # whether the checkout (our Libertine) sorts before the venv (matplotlib's own STIX, DejaVu). Every font a figure
    # can use is therefore registered from one private dir, copied there (not linked: matplotlib resolves links to real
    # paths): the order is then by file name, wherever the checkout and the venv are. The dir is removed at exit.
    # Done once per process: a script that draws several figures calls this for each.
    global _FONT_DIR
    if _FONT_DIR is None:
        _FONT_DIR = one = tempfile.mkdtemp(prefix="paper-fonts-")
        atexit.register(shutil.rmtree, one, True)
        bundled = Path(get_data_path()) / "fonts" / "ttf"
        sources = [*FONT_FILES.values(), *sorted(bundled.glob("*.ttf"))]
        fm = font_manager.fontManager
        # drop every font matplotlib found, its own and the system's: a system font with one of these names (a system
        # STIX, Libertine or DejaVu) would otherwise win a tie with our copy, and change the PDF bytes
        fm.ttflist = []
        for src in sources:
            copy = os.path.join(one, src.name)
            shutil.copyfile(src, copy)
            fm.addfont(copy)
    one = _FONT_DIR
    plt.rcParams.update({"font.family": "serif", "font.serif": [SERIF], "mathtext.fontset": "custom",
                         "mathtext.rm": SERIF, "mathtext.it": f"{SERIF}:italic", "mathtext.bf": f"{SERIF}:bold",
                         # no figure uses \mathcal, \mathsf or \mathtt; the custom fontset still looks their families up,
                         # and their defaults (cursive, sans, monospace) are system fonts
                         "mathtext.cal": SERIF, "mathtext.sf": SERIF, "mathtext.tt": SERIF,
                         "mathtext.fallback": "stix"})
    for (weight, style), path in FONT_FILES.items():
        try:
            got = font_manager.findfont(font_manager.FontProperties(family="serif", weight=weight, style=style),
                                        fallback_to_default=False)
        except ValueError as e:
            got = f"nothing ({e})"
        if os.path.realpath(got) != os.path.realpath(os.path.join(one, path.name)):
            sys.exit(f"paper font: the {weight} {style} serif resolves to {got}, not {path} (copied into {one})")
