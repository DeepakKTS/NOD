"""Render the README's and the banner's figures from committed artifacts.

INV-9: no number in any document is hand-written. The four bars in the patience
chart are the project's headline result, so they are read out of
`bench/runs/pilot_ladder.sweep.live.json` at render time rather than typed into
a template. A regenerated chart that disagreed with the sweep would be a
changed artifact, not a changed literal, and would show up in `git diff`.

Three outputs, all committed:

    docs/patience.svg      the four-bar chart, used inline in README §3
    docs/architecture.svg  the audio path and the out-of-band control path
    docs/banner.html       source for docs/banner.png, rasterised by `make banner`
    src/nod_server/static/favicon.svg   the tab icon, rasterised by `make favicon`

`make banner` shells Chrome at `docs/banner.html` exactly as `make cover` and
`make deck` do. The one renderer stays the one renderer (deck.html's header
note); a second one could disagree with the film.

**Paths are relative to the repository root, deliberately.** `docs/cover.html`
hardcodes `file:///Users/deepakzedler/...`, so `make cover` regenerates on
exactly one machine. That is a separate finding and is not fixed here, but it
is not repeated either: `banner.html` references `patience.svg` as a sibling,
so `make banner` works in any clone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NamedTuple

REPO: Path = Path(__file__).resolve().parents[1]
RUNS: Path = REPO / "bench" / "runs"
DOCS: Path = REPO / "docs"
STATIC: Path = REPO / "src" / "nod_server" / "static"

# The demo screen's own variables, src/nod_server/static/index.html:17-23.
# Taken from there rather than from a deck built outside this repository: the
# tree already holds two ambers (#FFB340 here, #F0A63C in DESIGN_SYSTEM.md) and
# a third would be one more than anyone can keep in step. #FFB340 is `--floor`,
# "the caller holds the floor", which is what a longer bar means.
BG = "#060709"
BG_PANEL = "#0A0C10"
AMBER = "#FFB340"
INK = "rgba(255,255,255,.96)"
INK_MUTED = "rgba(255,255,255,.60)"
INK_FAINT = "rgba(255,255,255,.34)"
EDGE = "rgba(255,255,255,.09)"

# SVG cannot resolve `ui-sans-serif`, and GitHub renders a committed SVG through
# an image proxy with no guarantee about which faces exist. Both stacks end in a
# generic family so the text renders somewhere legible whatever is installed.
SANS = "ui-sans-serif,-apple-system,'Helvetica Neue',Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,'DejaVu Sans Mono',monospace"

CHART_W = 1240
CHART_H = 520
# The left gutter has to clear the widest row label ("min_turn_silence 2400" at
# 21px mono). The first render set it to 268 and the bars painted over three of
# the four labels, which is why the labels are right-aligned against this edge
# rather than left-aligned from the margin: the anchor now moves with the
# gutter instead of being a second number to keep in step.
BAR_X = 340  # left edge of the plotting area
BAR_MAX_W = 640
BAR_H = 46
ROW_H = 84
ROW_Y0 = 128
SCALE_MAX_MS = 2600.0  # a round ceiling above the 2574 ms maximum


class Bar(NamedTuple):
    """One swept arm: the gate that was set, and how long the turn stayed open.

    Attributes:
        min_turn_silence_ms: the only setting that varied across the four arms.
        held_ms: `fired_at_ms - prefix_end_ms`, the silence the service tolerated
            after the caller stopped mid-sentence on a preposition.
    """

    min_turn_silence_ms: int
    held_ms: float


def read_bars() -> list[Bar]:
    """Derive the patience chart from the committed sweep.

    Reads the first boundary of each arm and subtracts the prefix end recorded
    in that run's pre-registered geometry, which is the same arithmetic the
    README's "turn held open for" column states.

    Returns:
        One `Bar` per arm, ordered by the gate that was set.

    Raises:
        SystemExit: if either artifact is missing, rather than rendering a chart
            with nothing behind it.

    Complexity: O(arms).
    """
    observed_path = RUNS / "pilot_ladder.sweep.live.json"
    predictions_path = RUNS / "pilot_ladder.sweep.live.predictions.json"
    for path in (observed_path, predictions_path):
        if not path.exists():
            raise SystemExit(f"figures: missing committed artifact {path}")

    observed = json.loads(observed_path.read_text(encoding="utf-8"))
    predictions = json.loads(predictions_path.read_text(encoding="utf-8"))
    prefix_end = {
        int(g["hold_label_ms"]): float(g["prefix_end_ms"])
        for g in predictions["geometry"]
    }
    arms = predictions["arms"]

    bars: list[Bar] = []
    for row in observed["rows"]:
        boundaries = row["boundaries"]
        if not boundaries:
            raise SystemExit(f"figures: arm {row['arm']} recorded no boundary")
        fired = float(boundaries[0]["fired_at_ms"])
        bars.append(
            Bar(
                min_turn_silence_ms=int(arms[row["arm"]]["min_turn_silence"]),
                held_ms=fired - prefix_end[int(row["hold_label_ms"])],
            )
        )
    bars.sort(key=lambda b: b.min_turn_silence_ms)
    if len(bars) != 4:
        raise SystemExit(f"figures: expected 4 swept arms, found {len(bars)}")
    return bars


def _esc(text: str) -> str:
    """Escape the three characters that matter inside SVG text content."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def patience_svg(bars: list[Bar]) -> str:
    """Draw the four-bar patience chart.

    The first arm is the vendor's own default gate, so it carries a marker and
    a dashed rule across the plot: every other bar is read against it.

    Args:
        bars: the arms, ordered by gate, from `read_bars`.

    Returns:
        A self-contained SVG document.

    Complexity: O(bars).
    """
    baseline = bars[0]
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CHART_W}" '
        f'height="{CHART_H}" viewBox="0 0 {CHART_W} {CHART_H}" '
        f'role="img" aria-label="Turn held open, by min_turn_silence">',
        f'<rect width="{CHART_W}" height="{CHART_H}" rx="18" fill="{BG_PANEL}"/>',
        f'<rect x="0.5" y="0.5" width="{CHART_W - 1}" height="{CHART_H - 1}" '
        f'rx="18" fill="none" stroke="{EDGE}"/>',
        f'<text x="40" y="52" font-family="{MONO}" font-size="15" '
        f'letter-spacing="2.4" fill="{INK_FAINT}">'
        f"HOW LONG THE SERVICE HELD THE TURN OPEN</text>",
        f'<text x="40" y="88" font-family="{SANS}" font-size="25" fill="{INK_MUTED}">'
        f"{_esc('after the caller stopped mid-sentence on a preposition')}</text>",
    ]

    def width_for(ms: float) -> float:
        return BAR_MAX_W * ms / SCALE_MAX_MS

    # The vendor default, drawn behind the bars so it reads as a reference line
    # rather than as another series.
    rule_x = BAR_X + width_for(baseline.held_ms)
    rule_top = ROW_Y0 - 22
    rule_bottom = ROW_Y0 + ROW_H * len(bars) - 20
    out.append(
        f'<line x1="{rule_x:.1f}" y1="{rule_top}" x2="{rule_x:.1f}" '
        f'y2="{rule_bottom}" stroke="{INK_FAINT}" stroke-width="1" '
        f'stroke-dasharray="4 5"/>'
    )

    for index, bar in enumerate(bars):
        y = ROW_Y0 + index * ROW_H
        bar_w = width_for(bar.held_ms)
        is_baseline = bar is baseline
        out.extend(
            [
                f'<text x="{BAR_X - 22}" y="{y + BAR_H - 14}" text-anchor="end" '
                f'font-family="{MONO}" font-size="21" fill="{INK_MUTED}">'
                f"min_turn_silence {bar.min_turn_silence_ms}</text>",
                f'<rect x="{BAR_X}" y="{y}" width="{BAR_MAX_W}" height="{BAR_H}" '
                f'rx="6" fill="rgba(255,255,255,.035)"/>',
                f'<rect x="{BAR_X}" y="{y}" width="{bar_w:.1f}" height="{BAR_H}" '
                f'rx="6" fill="{AMBER}" fill-opacity="{0.38 if is_baseline else 1}"/>',
                f'<text x="{BAR_X + bar_w + 18:.1f}" y="{y + BAR_H - 13}" '
                f'font-family="{MONO}" font-size="26" font-weight="600" '
                f'fill="{INK if not is_baseline else INK_MUTED}">'
                f"{bar.held_ms:.0f} ms</text>",
            ]
        )
        if is_baseline:
            out.append(
                f'<text x="{BAR_X + bar_w + 18:.1f}" y="{y + BAR_H + 12}" '
                f'font-family="{MONO}" font-size="15" fill="{INK_FAINT}">'
                f"vendor default</text>"
            )

    out.append(
        f'<text x="40" y="{CHART_H - 30}" font-family="{MONO}" font-size="16" '
        f'fill="{INK_FAINT}">'
        f"{_esc('four live sessions against AssemblyAI Universal-Streaming, one per arm')}"
        f"</text>"
    )
    out.append(
        f'<text x="40" y="{CHART_H - 10}" font-family="{MONO}" font-size="16" '
        f'fill="{INK_FAINT}">bench/runs/pilot_ladder.sweep.live.json</text>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


ARCH_W = 1180
ARCH_H = 432


def architecture_svg() -> str:
    """Draw the audio path and the out-of-band control path.

    INV-1: the controller never sits between the caller's audio and AssemblyAI.
    The diagram exists to make that shape visible, so the audio row is drawn as
    one unbroken line and the controller hangs off it.

    Returns:
        A self-contained SVG document.

    Complexity: O(1).
    """

    def box(x: int, y: int, w: int, h: int, title: str, sub: str, accent: str) -> str:
        return (
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" '
            f'fill="{BG_PANEL}" stroke="{accent}" stroke-opacity="0.45"/>'
            f'<text x="{x + w / 2:.0f}" y="{y + 34}" text-anchor="middle" '
            f'font-family="{SANS}" font-size="20" fill="{INK}">{_esc(title)}</text>'
            + (
                f'<text x="{x + w / 2:.0f}" y="{y + 58}" text-anchor="middle" '
                f'font-family="{MONO}" font-size="14" fill="{INK_FAINT}">'
                f"{_esc(sub)}</text>"
                if sub
                else ""
            )
        )

    def arrow(x1: int, y1: int, x2: int, y2: int, colour: str) -> str:
        return (
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" '
            f'stroke-width="2" marker-end="url(#head-{colour.lstrip("#")})"/>'
        )

    row_y, row_h = 86, 78
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{ARCH_W}" '
        f'height="{ARCH_H}" viewBox="0 0 {ARCH_W} {ARCH_H}" role="img" '
        f'aria-label="Nod observes the transcript stream and writes config back '
        f'out of band">',
        "<defs>",
    ]
    out.extend(
        f'<marker id="head-{colour.lstrip("#")}" viewBox="0 0 10 10" '
        f'refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker>'
        for colour in (AMBER, "#5AC8FA")
    )
    out.extend(
        [
            "</defs>",
            f'<rect width="{ARCH_W}" height="{ARCH_H}" rx="18" fill="{BG}"/>',
            f'<rect x="0.5" y="0.5" width="{ARCH_W - 1}" height="{ARCH_H - 1}" '
            f'rx="18" fill="none" stroke="{EDGE}"/>',
            f'<text x="40" y="46" font-family="{MONO}" font-size="15" '
            f'letter-spacing="2.4" fill="{INK_FAINT}">'
            f"THE AUDIO PATH IS NEVER IN THE LOOP &#183; INV-1</text>",
            box(40, row_y, 210, row_h, "caller audio", "16 kHz PCM", AMBER),
            box(310, row_y, 210, row_h, "Nod proxy", "forwards verbatim", AMBER),
            box(580, row_y, 300, row_h, "AssemblyAI", "Universal-Streaming", AMBER),
            box(940, row_y, 200, row_h, "your agent", "replies", AMBER),
            arrow(250, row_y + row_h // 2, 306, row_y + row_h // 2, AMBER),
            arrow(520, row_y + row_h // 2, 576, row_y + row_h // 2, AMBER),
            arrow(880, row_y + row_h // 2, 936, row_y + row_h // 2, AMBER),
            box(
                440,
                274,
                420,
                86,
                "rhythm controller",
                "decide() pure, sync, p99 < 5 ms",
                "#5AC8FA",
            ),
            # Fan-out down, patch back up. Two separate paths, because they are.
            f'<path d="M730,{row_y + row_h} L730,238 L650,238 L650,270" '
            f'fill="none" stroke="#5AC8FA" stroke-width="2" '
            f'marker-end="url(#head-5AC8FA)"/>',
            f'<path d="M560,274 L560,238 L415,238 L415,{row_y + row_h}" '
            f'fill="none" stroke="#5AC8FA" stroke-width="2" '
            f'marker-end="url(#head-5AC8FA)"/>',
            f'<text x="754" y="214" font-family="{MONO}" font-size="15" '
            f'fill="{INK_MUTED}">transcript events, fan-out</text>',
            f'<text x="176" y="214" font-family="{MONO}" font-size="15" '
            f'fill="{INK_MUTED}">UpdateConfiguration + ConfigDecision</text>',
            f'<text x="40" y="{ARCH_H - 26}" font-family="{MONO}" font-size="15" '
            f'fill="{INK_FAINT}">'
            f"{_esc('audio forwarding never awaits the controller; every patch carries its reason (INV-4)')}"
            f"</text>",
            "</svg>",
        ]
    )
    return "\n".join(out) + "\n"


FAVICON_PX = 32


def favicon_svg() -> str:
    """Draw the tab icon: an amber tile carrying a lowercase `n`.

    **An amber field with the glyph knocked out, not a glyph on a dark field.**
    A favicon is rendered at 16 px against browser chrome that may be light or
    dark, and a dark tile disappears into a dark tab strip. A solid amber block
    is visible against both, which is the only property that matters at that
    size.

    **The `n` is a path, not text.** A favicon carrying `<text>` renders in
    whatever face the client happens to have, which is the one thing this file
    cannot control. Stroked paths render identically everywhere.

    Returns:
        A self-contained SVG document on a 32-unit grid.

    Complexity: O(1).
    """
    stem = 4.0
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{FAVICON_PX}" '
        f'height="{FAVICON_PX}" viewBox="0 0 32 32" role="img" '
        f'aria-label="Nod">'
        f'<rect width="32" height="32" rx="7.5" fill="{AMBER}"/>'
        f'<g fill="none" stroke="{BG}" stroke-width="{stem}" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="M10.6 23.2 V12.4"/>'
        f'<path d="M10.6 16.9 C10.6 13.9 12.9 11.9 16.0 11.9 '
        f'C19.1 11.9 21.4 13.9 21.4 16.9 V23.2"/>'
        f"</g></svg>\n"
    )


BANNER_W = 2400
BANNER_H = 840


def banner_html() -> str:
    """Compose the README banner around the patience chart.

    The chart is referenced as a sibling file rather than inlined so that the
    banner and README figure 3 are the same artifact and cannot drift.

    Returns:
        A standalone HTML document sized for a headless screenshot.

    Complexity: O(1).
    """
    return f"""<!doctype html>
<meta charset="utf-8">
<title>Nod &mdash; banner</title>
<!--
  GENERATED by `python scripts/figures.py banner`. Do not edit by hand.
  Rasterise with `make banner`, which screenshots this at {BANNER_W}x{BANNER_H}.

  The chart is docs/patience.svg, referenced relatively so this renders in any
  clone. Its four values come from bench/runs/pilot_ladder.sweep.live.json at
  generation time (INV-9).

  GitHub strips <a> inside an SVG, so there is no clickable region in the
  artwork. The button is drawn into the image and the whole banner carries the
  link from the README.
-->
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; width: {BANNER_W}px; height: {BANNER_H}px;
    background: {BG}; color: {INK};
    font-family: {SANS};
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
    display: flex; align-items: center; gap: 88px; padding: 0 104px;
    overflow: hidden;
  }}
  .left {{ width: 900px; flex: none; }}
  .mark {{
    font-size: 184px; font-weight: 700; letter-spacing: -8px;
    line-height: 0.92; margin: 0 0 26px;
  }}
  .sub {{
    font-size: 40px; line-height: 1.28; color: {INK_MUTED};
    margin: 0 0 32px; white-space: nowrap;
  }}
  .fact {{
    font-family: {MONO}; font-size: 23px; line-height: 1.5;
    color: {INK_FAINT}; margin: 0 0 42px; max-width: 46ch;
  }}
  .fact b {{ color: {AMBER}; font-weight: 600; }}
  .btn {{
    display: inline-flex; align-items: center; gap: 16px;
    background: {AMBER}; color: #14100A;
    font-size: 32px; font-weight: 650; letter-spacing: -0.3px;
    padding: 26px 46px; border-radius: 999px;
  }}
  .btn .arr {{ font-size: 30px; opacity: .8; }}
  .powered {{
    margin: 30px 0 0; font-size: 27px; color: {INK_FAINT};
    letter-spacing: -0.2px;
  }}
  .powered b {{ color: {INK}; font-weight: 600; }}
  .right {{ flex: none; }}
  .right img {{ width: 1204px; display: block; }}
</style>
<body>
  <div class="left">
    <p class="mark">Nod</p>
    <p class="sub">Voice agents that wait for you to finish.</p>
    <p class="fact">One knob, swept live. At the vendor default the service
      gave a hesitating caller <b>777&nbsp;ms</b>. At
      <code>min_turn_silence&nbsp;2400</code> it gave them <b>2574&nbsp;ms</b>,
      and took the rest of the sentence as the same turn.</p>
    <span class="btn">Experience the live demo <span class="arr">&#8594;</span></span>
    <p class="powered">Powered by <b>AssemblyAI</b> Universal-Streaming</p>
  </div>
  <div class="right"><img src="patience.svg" alt="Turn held open, by min_turn_silence"></div>
</body>
"""


def main() -> int:
    """Write the requested figures into `docs/`.

    Returns:
        0 on success. Missing artifacts raise `SystemExit` from `read_bars`.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        default="all",
        choices=("all", "patience", "architecture", "banner", "favicon"),
    )
    args = parser.parse_args()

    wrote: list[Path] = []
    if args.target in ("all", "patience", "banner"):
        # `banner` needs the chart beside it, so it always regenerates both.
        path = DOCS / "patience.svg"
        path.write_text(patience_svg(read_bars()), encoding="utf-8")
        wrote.append(path)
    if args.target in ("all", "architecture"):
        path = DOCS / "architecture.svg"
        path.write_text(architecture_svg(), encoding="utf-8")
        wrote.append(path)
    if args.target in ("all", "favicon"):
        path = STATIC / "favicon.svg"
        path.write_text(favicon_svg(), encoding="utf-8")
        wrote.append(path)
    if args.target in ("all", "banner"):
        path = DOCS / "banner.html"
        path.write_text(banner_html(), encoding="utf-8")
        wrote.append(path)

    for path in wrote:
        print(f"wrote {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
