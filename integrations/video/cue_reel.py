"""Cue reels from a short script: real footage of Cue's design prototype and Tock,
big Archivo headlines, sound effects on every cut. 1080x1920, H.264 + AAC.

Assets ship in assets/ (see assets/README.md for where each came from):
  assets/cue/clips/*.mp4   1080x1080 screen recordings of Cue's prototype and Tock
  assets/sfx/*.mp3         sound effects from the Motion-as-code kit
  assets/fonts/*.ttf       Archivo and IBM Plex Mono (SIL OFL)

A script is {"hook": str, "beats": [{"text": str, "clip": str, "secs": float, "from": float}], "cta": str}
("from" = where in the clip the beat starts, default 0).
"""
from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

from workforce.policies.engine import IntegrationError

ASSETS = Path(__file__).resolve().parents[2] / "assets"
W, H, FPS = 1080, 1920, 30
BG, INK, AMBER, MUTED = "0x0d0f13", "0xf2efe9", "0xffb347", "0xa5a8ae"
HOOK_CLIP, CTA_CLIP = "hover", "tock-task-done"
SITE = "cue.mithunsidhaarth.in"


def clips(assets: Path = ASSETS) -> list[str]:
    return sorted(p.stem for p in (assets / "cue" / "clips").glob("*.mp4"))


def check_script(script: dict, assets: Path = ASSETS) -> list[str]:
    """Problems with a script; empty when it can be rendered."""
    have, problems = set(clips(assets)), []
    if not str(script.get("hook") or "").strip():
        problems.append("no hook")
    beats = script.get("beats") or []
    if not 2 <= len(beats) <= 5:
        problems.append("needs 2-5 beats")
    for b in beats:
        if b.get("clip") not in have:
            problems.append(f"unknown clip {b.get('clip')!r}")
        if len(str(b.get("text") or "")) > 60:
            problems.append(f"beat text too long: {b.get('text')!r}")
    total = 2.4 + sum(float(b.get("secs") or 3) for b in beats) + 2.8
    if not 7 <= total <= 40:
        problems.append(f"length {total:.0f}s outside 7-40s")
    return problems


def _ff(args: list[str], timeout: float = 240) -> None:
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise IntegrationError(f"ffmpeg failed: {p.stderr[-400:]}")


def _wrap(text: str, width: int) -> str:
    return "\n".join(textwrap.wrap(" ".join(str(text).split()), width=width)) or " "


def _segment(tmp: Path, i: int, text: str, clip: str, secs: float, assets: Path, big: bool, start: float = 0.0) -> Path:
    size, width = (92, 13) if big else (74, 17)
    lines = _wrap(text, width).split("\n")[:4]
    f_head, f_mono = assets / "fonts" / "Archivo-w1125-900.ttf", assets / "fonts" / "IBMPlexMono-Regular.ttf"
    foot, tag = tmp / "foot.txt", tmp / "tag.txt"
    foot.write_text(SITE, encoding="utf-8")
    tag.write_text("CUE  ·  A NOTCH FOR WINDOWS", encoding="utf-8")
    step = size + 18
    top = 230 + (420 - step * len(lines)) // 2
    heads = []
    for n, line in enumerate(lines):          # one drawtext per line so every line is centred
        lf = tmp / f"t{i}_{n}.txt"
        lf.write_text(line, encoding="utf-8")
        heads.append(f"drawtext=fontfile='{f_head}':textfile='{lf}':expansion=none:fontsize={size}:fontcolor={INK}:"
                     f"x=(w-text_w)/2:y={top + n * step}:alpha='min(1,max(0,(t-{0.06 * n:.2f})/0.22))'")
    out = tmp / f"s{i}.mp4"
    vf = (f"[1:v]scale={W}:{W},fade=in:st=0:d=0.18[c];"
          f"[0:v][c]overlay=0:700[b];"
          f"[b]drawbox=x=0:y=698:w={W}:h=2:color={AMBER}@0.35:t=fill,"
          f"drawtext=fontfile='{f_mono}':textfile='{tag}':expansion=none:fontsize=30:fontcolor={MUTED}:x=(w-text_w)/2:y=150,"
          + ",".join(heads) + ","
          f"drawbox=x={W // 2 - 60}:y=640:w=120:h=6:color={AMBER}:t=fill,"
          f"drawtext=fontfile='{f_mono}':textfile='{foot}':expansion=none:fontsize=36:fontcolor={AMBER}:x=(w-text_w)/2:y=1830,"
          f"format=yuv420p[v]")
    _ff(["-f", "lavfi", "-i", f"color=c={BG}:s={W}x{H}:d={secs}:r={FPS}",
         "-ss", f"{start}", "-stream_loop", "-1", "-t", f"{secs}", "-i", str(assets / "cue" / "clips" / f"{clip}.mp4"),
         "-filter_complex", vf, "-map", "[v]", "-t", f"{secs}", "-r", str(FPS),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-an", str(out)])
    return out


def render_cue_reel(script: dict, out_path: str | Path, assets: Path = ASSETS) -> dict:
    problems = check_script(script, assets)
    if problems:
        raise IntegrationError("script can't be rendered: " + "; ".join(problems))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plan = [(script["hook"], script.get("hook_clip") or HOOK_CLIP, 2.4, True, 0.0)]
    plan += [(b["text"], b["clip"], float(b.get("secs") or 3), False, float(b.get("from") or 0)) for b in script["beats"]]
    plan += [(script.get("cta") or "Link in bio.", CTA_CLIP, 2.8, True, 0.0)]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        segs = [_segment(tmp, i, txt, clip, secs, assets, big, start) for i, (txt, clip, secs, big, start) in enumerate(plan)]
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{s}'\n" for s in segs), encoding="utf-8")
        video = tmp / "video.mp4"
        _ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(video)])
        # sound: a whoosh on every cut, a blip as each line lands, a riser into the hook, a chime on the end card
        sfx, t = [("riser_1", 0.0, 0.5), ("impact_small_1", 0.15, 0.7)], 0.0
        for n, (_, _, secs, _, _) in enumerate(plan):
            if n:
                sfx += [("whoosh_soft_1", max(0.0, t - 0.12), 0.8), ("ui_blip_1" if n % 2 else "ui_tick_1", t + 0.2, 0.6)]
            t += secs
        sfx.append(("confirm_chime_1", t - 2.4, 0.7))
        ins, chains = [], []
        for k, (name, at, vol) in enumerate(sfx):
            ins += ["-i", str(assets / "sfx" / f"{name}.mp3")]
            chains.append(f"[{k + 1}:a]volume={vol},adelay={int(at * 1000)}|{int(at * 1000)}[a{k}]")
        mix = ";".join(chains) + ";" + "".join(f"[a{k}]" for k in range(len(sfx))) + \
            f"amix=inputs={len(sfx)}:normalize=0,apad,atrim=0:{t:.2f},afade=t=out:st={t - 0.6:.2f}:d=0.6[a]"
        _ff(["-i", str(video), *ins, "-filter_complex", mix, "-map", "0:v", "-map", "[a]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-shortest", "-movflags", "+faststart", str(out_path)])
    from .render import validate_file
    return {"path": str(out_path), **validate_file(out_path)}
