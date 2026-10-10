"""Turn the raw demo recording into README media (needs ffmpeg on PATH).

    python scripts/make_demo_media.py [.cache/demo] [docs/media]

Reads demo.webm + demo-segments.json written by apps/dashboard/e2e/demo.mjs. Waiting
spans (alert `for:` windows, rollout, verification) are sped up; interaction is kept
close to real time. Writes demo.mp4 (H.264) and demo.gif (for inline display on GitHub).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

NORMAL_SPEED = 1.25
FAST_SPEED = 20.0
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def duration(path: Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return float(out.strip())


def plan(total: float, fast: list[dict[str, float]]) -> list[tuple[float, float, float]]:
    """(start, end, speed) spans covering the whole recording."""
    spans: list[tuple[float, float, float]] = []
    t = 0.0
    for seg in sorted(fast, key=lambda s: s["start"]):
        if seg["end"] - seg["start"] < 2:
            continue  # too short to bother
        if seg["start"] > t:
            spans.append((t, seg["start"], NORMAL_SPEED))
        spans.append((seg["start"], seg["end"], FAST_SPEED))
        t = seg["end"]
    if total > t:
        spans.append((t, total, NORMAL_SPEED))
    return spans


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".cache/demo")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/media")
    dst.mkdir(parents=True, exist_ok=True)
    video = src / "demo.webm"
    spans = plan(duration(video), json.loads((src / "demo-segments.json").read_text())["fast"])

    parts = [
        f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{speed}[v{i}]"
        for i, (a, b, speed) in enumerate(spans)
    ]
    concat = "".join(f"[v{i}]" for i in range(len(spans))) + f"concat=n={len(spans)}:v=1[out]"
    graph = ";".join([*parts, concat])
    mp4 = dst / "demo.mp4"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", video, "-filter_complex", graph, "-map", "[out]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-preset", "slow",
         "-movflags", "+faststart", mp4],
        check=True,
    )  # fmt: skip
    gif = dst / "demo.gif"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", mp4, "-vf",
         "fps=8,scale=960:-1:flags=lanczos,split[a][b];"
         "[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
         gif],
        check=True,
    )  # fmt: skip
    for f in (mp4, gif):
        print(f"{f}: {f.stat().st_size / 1e6:.1f} MB, {duration(f):.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
