"""Assemble the README demo GIF from the screencast the Playwright recorder wrote.

Two steps on purpose: Playwright owns "what the console looked like", this owns
"how long each beat is held". Re-timing the demo does not mean re-recording it.

    cd frontend && YBM_RECORD_DEMO=1 npx playwright test demo.spec.ts
    backend/.venv/Scripts/python scripts/make_demo_gif.py

The recorder writes JPEG frames with the browser's own timestamps (times.json)
and named moments (marks.json). This samples them at a steady frame rate, in
real time, and holds the beats a reader needs a moment for.

Pillow only - no ffmpeg, no imagemagick, nothing a contributor has to install
beyond what `ybm setup` already put in the backend venv.
"""

from __future__ import annotations

import bisect
import json
from pathlib import Path
import sys

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
FRAME_DIR = REPO_ROOT / "frontend" / ".demo-frames"
OUTPUT = REPO_ROOT / "docs" / "screenshots" / "demo.gif"

# Rendered at 2400 wide; GitHub shows the README at roughly 880, and every
# pixel here is paid for on every page load.
TARGET_WIDTH = 880
FPS = 10

# Extra time on a beat, in milliseconds: (mark, seconds after it, hold). The
# approval is the one the whole demo exists for - the difference between "an
# agent ran a command" and "an agent asked first" - so it gets the longest.
HOLDS = [
    ("approval", 2.6, 1600),
    ("done", 1.4, 1800),
    ("end", 0.0, 1200),
]


def load() -> tuple[list[dict], dict[str, float]]:
    times_file = FRAME_DIR / "times.json"
    if not times_file.exists():
        raise SystemExit(
            f"No recording in {FRAME_DIR}.\n"
            "Record it first: cd frontend && YBM_RECORD_DEMO=1 npx playwright test demo.spec.ts"
        )
    times = json.loads(times_file.read_text(encoding="utf-8"))
    marks = json.loads((FRAME_DIR / "marks.json").read_text(encoding="utf-8"))
    return times, marks


def frame_at(times: list[dict], starts: list[float], t: float) -> Path:
    """The frame on screen at time t: the last one the browser painted at or before it."""
    index = max(0, bisect.bisect_right(starts, t) - 1)
    return FRAME_DIR / times[index]["file"]


def build_palette(sheet: Image.Image, accents: int = 40) -> Image.Image:
    """A 256-color palette: median cut for the console's many dark tones, plus slots kept for the
    small saturated accents (the red risk badge, the amber banner, the green check) that median
    cut would merge into the greys around them."""
    tones = sheet.quantize(colors=256 - accents, method=Image.Quantize.MEDIANCUT)
    hsv = sheet.convert("HSV")
    vivid = [
        rgb
        for rgb, (_, saturation, value) in zip(sheet.get_flattened_data(), hsv.get_flattened_data())
        if saturation > 70 and value > 60
    ]
    colors = (tones.getpalette() or [])[: 3 * (256 - accents)]
    if vivid:
        strip = Image.new("RGB", (len(vivid), 1))
        strip.putdata(vivid)
        pick = strip.quantize(colors=accents, method=Image.Quantize.MAXCOVERAGE)
        colors += (pick.getpalette() or [])[: 3 * accents]
    colors += [0] * (768 - len(colors))
    palette = Image.new("P", (1, 1))
    palette.putpalette(colors[:768])
    return palette


def main() -> int:
    times, marks = load()
    starts = [frame["t"] for frame in times]
    end = marks.get("end", starts[-1])
    step = 1.0 / FPS
    samples = [k * step for k in range(int(end / step) + 1)]
    holds = {}
    for mark, after, extra in HOLDS:
        at = min(samples, key=lambda s: abs(s - (marks[mark] + after)))
        holds[at] = holds.get(at, 0) + extra

    images, durations, previous = [], [], None
    cache: dict[Path, Image.Image] = {}
    for t in samples:
        path = frame_at(times, starts, t)
        duration = round(1000 * step) + holds.get(t, 0)
        if path == previous and not holds.get(t):
            durations[-1] += duration  # nothing changed on screen: lengthen the last frame
            continue
        if path not in cache:
            image = Image.open(path).convert("RGB")
            height = round(image.height * TARGET_WIDTH / image.width)
            cache[path] = image.resize((TARGET_WIDTH, height), Image.LANCZOS)
        images.append(cache[path])
        durations.append(duration)
        previous = path

    # One palette for the whole GIF, taken from the frames where the most is on screen (the
    # approval and the receipt): frames that share it compress far better than per-frame ones,
    # and the console's flat colors stay clean without dithering.
    keys = [("typing", 2.0), ("approval", 2.6), ("approved", 0.6), ("done", 1.4)]
    sheet = Image.new("RGB", (TARGET_WIDTH, images[0].height * len(keys)))
    for row, (mark, after) in enumerate(keys):
        key = frame_at(times, starts, marks[mark] + after)
        sheet.paste(cache[key] if key in cache else images[-1], (0, row * images[0].height))
    palette = build_palette(sheet)
    quantized = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    quantized[0].save(
        OUTPUT,
        save_all=True,
        append_images=quantized[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")
    print(
        f"  {len(images)} frames, {images[0].width}x{images[0].height}, "
        f"{sum(durations) / 1000:.1f} s, {size_kb:.0f} KB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
