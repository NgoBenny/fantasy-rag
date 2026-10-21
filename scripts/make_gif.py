"""Assemble docs/demo.gif from the frames e2e/gif.demo.spec.ts records (DEMO_MODE only).

Frame files are named NNN_<milliseconds>.png; each is shown for that long. Run from
backend/: uv run python ../scripts/make_gif.py
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
FRAMES = ROOT / "frontend" / "test-results" / "gif"
OUT = ROOT / "docs" / "demo.gif"
WIDTH = 880  # keeps the GIF small enough for the README


def main() -> None:
    files = sorted(FRAMES.glob("*.png"))
    if not files:
        raise SystemExit(f"no frames in {FRAMES}; record them first (see the spec)")
    frames, durations = [], []
    for f in files:
        img = Image.open(f).convert("RGB")
        img = img.resize((WIDTH, round(img.height * WIDTH / img.width)), Image.Resampling.LANCZOS)
        frames.append(img.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
        durations.append(int(f.stem.split("_")[1]))
    frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   optimize=True)  # fmt: skip
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
