"""Generated pictures, scans and speech for the challenge and verdict suites: nothing is downloaded and no real person's data is used.

Every helper returns whether it worked. A machine without a speech voice or a Japanese font gets an empty value for that fixture, and the
runner skips the cases that need it instead of failing them.
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

_FONT_DIRS = (Path("C:/Windows/Fonts"), Path("/usr/share/fonts"), Path("/Library/Fonts"))


def _font(size: int, *, japanese: bool = False):
    from PIL import ImageFont

    names = ("YuGothM.ttc", "msgothic.ttc", "meiryo.ttc") if japanese else ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf")
    for folder in _FONT_DIRS:
        for name in names:
            candidate = folder / name
            if candidate.exists():
                return ImageFont.truetype(str(candidate), size)
    if japanese:
        return None
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow before 10.1 draws only the small built-in font
        return ImageFont.load_default()


def text_image(path: Path, lines: list[str], *, size: tuple[int, int] = (900, 560), font_size: int = 38, japanese: bool = False) -> bool:
    """A white page with black lines of text. Returns False when no font can draw it."""
    from PIL import Image, ImageDraw

    font = _font(font_size, japanese=japanese)
    if font is None:
        return False
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    y = 30
    for line in lines:
        draw.text((40, y), line, fill="black", font=font)
        y += font_size + 14
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return True


def scanned_pdf(path: Path, lines: list[str]) -> bool:
    """A one-page PDF that is only a picture of text: no text layer, as a scanner makes it."""
    from PIL import Image

    scratch = path.with_suffix(".scan.png")
    if not text_image(scratch, lines):
        return False
    Image.open(scratch).convert("RGB").save(path, "PDF", resolution=100.0)
    scratch.unlink()
    return True


def sharp_photo(path: Path, seed: int) -> None:
    """A busy, high-contrast picture (edges everywhere), so a blur is measurable."""
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    image = Image.new("RGB", (640, 480), (230, 230, 230))
    draw = ImageDraw.Draw(image)
    for _ in range(140):
        x, y = rng.randrange(0, 600), rng.randrange(0, 440)
        draw.rectangle([x, y, x + rng.randrange(8, 60), y + rng.randrange(8, 60)], fill=tuple(rng.randrange(0, 255) for _ in range(3)))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=95)


def blurred_photo(path: Path, seed: int) -> None:
    from PIL import ImageFilter

    sharp_photo(path, seed)
    from PIL import Image

    Image.open(path).filter(ImageFilter.GaussianBlur(radius=9)).save(path, quality=95)


def sized_photo(path: Path, width: int, height: int, seed: int) -> None:
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    image = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    for _ in range(30):
        x, y = rng.randrange(0, width - 20), rng.randrange(0, height - 20)
        draw.ellipse([x, y, x + 20, y + 20], fill=tuple(rng.randrange(0, 255) for _ in range(3)))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def speak_wav(path: Path, text: str) -> bool:
    """Speech as a WAV, from the voice Windows already has. False when there is none (other systems, or no voice)."""
    script = (
        "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = -1; $s.SetOutputToWaveFile($args[0]); $s.Speak($args[1]); $s.Dispose()"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script, str(path), text],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and path.exists() and path.stat().st_size > 1000
