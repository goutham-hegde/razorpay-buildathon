"""How long the video script actually runs, per section.

A five-minute cap is a word budget, not an intention: at a natural reading pace it is about
750 words, and prose overshoots it without ever looking long on the page. This counts only
what gets *said* - headings, on-screen cues, fenced blocks and the preamble are excluded -
and breaks it down per section so an overrun can be cut where it is cheapest rather than
trimmed evenly out of everything.

    python docs/wordcount.py [--wpm 150] [path]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_SCRIPT = Path(__file__).with_name("video-script.md")

#: Where the spoken text starts and stops. Everything before the first timestamped heading
#: is preparation notes, and everything from the recording notes on is direction.
FIRST_SPOKEN = "## 0:00"
AFTER_SPOKEN = "## Notes for recording"


def spoken_sections(text: str) -> list[tuple[str, int]]:
    body = text.split(FIRST_SPOKEN, 1)[-1].split(AFTER_SPOKEN, 1)[0]
    body = FIRST_SPOKEN + body
    out: list[tuple[str, int]] = []
    for chunk in re.split(r"\n(?=## )", body):
        lines = chunk.splitlines()
        heading = lines[0].lstrip("# ").strip() if lines else ""
        rest = "\n".join(lines[1:])
        rest = re.sub(r"```.*?```", "", rest, flags=re.S)          # code blocks
        rest = "\n".join(l for l in rest.splitlines()
                         if not l.startswith((">", "---", "#")))    # cues and rules
        out.append((heading, len(rest.split())))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default=DEFAULT_SCRIPT, type=Path)
    ap.add_argument("--wpm", type=float, default=150.0, help="reading pace")
    ap.add_argument("--cap", type=float, default=5.0, help="target length in minutes")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sections = spoken_sections(args.path.read_text(encoding="utf-8"))
    total = sum(n for _, n in sections)

    for heading, n in sections:
        secs = n / args.wpm * 60
        print(f"  {n:5d}  {int(secs) // 60}:{int(secs) % 60:02d}   {heading}")

    secs = total / args.wpm * 60
    budget = int(args.cap * args.wpm)
    verdict = "over" if total > budget else "fits"
    print(f"\n  {total:5d}  {int(secs) // 60}:{int(secs) % 60:02d}   TOTAL at {args.wpm:.0f} wpm")
    print(f"  budget {budget} words for {args.cap:.0f}:00 - {verdict} by {abs(total - budget)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
