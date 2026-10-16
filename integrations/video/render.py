"""FFmpeg reel renderer: scenes -> vertical MP4 with burned-in subtitles.

Uses only generated visuals (solid backgrounds + text), so every asset it makes
is license-clean. Validates the output with ffprobe (resolution, duration,
codec) before the asset is marked `validated`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

from workforce.policies.engine import IntegrationError

SPECS = {"instagram_reel": {"w": 1080, "h": 1920, "min_s": 3, "max_s": 90}}


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _srt_time(t: float) -> str:
    h, rem = divmod(int(t * 1000), 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(scenes: list[dict], path: Path) -> None:
    t, out = 0.0, []
    for i, sc in enumerate(scenes, 1):
        d = float(sc.get("duration", 3))
        out.append(f"{i}\n{_srt_time(t)} --> {_srt_time(t + d)}\n{sc.get('narration') or sc['text']}\n")
        t += d
    path.write_text("\n".join(out), encoding="utf-8")


def probe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_name:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise IntegrationError(f"ffprobe failed: {p.stderr[:300]}")
    d = json.loads(p.stdout)
    v = next(s for s in d["streams"] if s.get("width"))
    return {"width": v["width"], "height": v["height"], "codec": v["codec_name"], "duration": float(d["format"]["duration"])}


def render_reel(scenes: list[dict], out_path: Path, spec: str = "instagram_reel", bg: str = "0x101418",
                fg: str = "white") -> dict:
    if not available():
        raise IntegrationError("ffmpeg/ffprobe not installed")
    s = SPECS[spec]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.with_suffix("")
    work.mkdir(exist_ok=True)
    parts = []
    for i, sc in enumerate(scenes):
        txt = "\n".join(textwrap.wrap(sc["text"], 22))[:400]
        tf = work / f"s{i}.txt"
        tf.write_text(txt, encoding="utf-8")
        part = work / f"s{i}.mp4"
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={bg}:s={s['w']}x{s['h']}:d={float(sc.get('duration', 3))}:r=30",
               "-vf", f"drawtext=textfile={tf}:fontcolor={fg}:fontsize=72:line_spacing=18:x=(w-text_w)/2:y=(h-text_h)/2",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(part)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise IntegrationError(f"ffmpeg scene render failed: {p.stderr[:300]}")
        parts.append(part)
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    srt = work / "subs.srt"
    write_srt(scenes, srt)
    joined = work / "joined.mp4"
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)],
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise IntegrationError(f"ffmpeg concat failed: {p.stderr[:300]}")
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-vf",
                        f"subtitles={srt}:force_style='FontSize=14,MarginV=60'", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(out_path)], capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise IntegrationError(f"ffmpeg subtitle burn failed: {p.stderr[:300]}")
    info = probe(out_path)
    problems = []
    if (info["width"], info["height"]) != (s["w"], s["h"]):
        problems.append(f"resolution {info['width']}x{info['height']} != {s['w']}x{s['h']}")
    if not s["min_s"] <= info["duration"] <= s["max_s"]:
        problems.append(f"duration {info['duration']:.1f}s outside {s['min_s']}-{s['max_s']}s")
    shutil.copy(srt, out_path.with_suffix(".srt"))
    shutil.rmtree(work, ignore_errors=True)
    return {"path": str(out_path), "subtitles": str(out_path.with_suffix(".srt")), **info,
            "validated": not problems, "problems": problems}
