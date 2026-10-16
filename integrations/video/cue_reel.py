"""Cue reels: full-screen footage of Cue and Tock, a hook sticker in the first
second, word-by-word captions synced to a voiceover, smooth transitions and
sound effects. 1080x1920, H.264 + AAC, loudness-normalised.

Assets ship in assets/ (see assets/README.md):
  assets/cue/clips/*.mp4   1080x1080 recordings of Cue's prototype and Tock
  assets/sfx/*.mp3         sound effects from the Motion-as-code kit
  assets/fonts/*.ttf       Archivo and IBM Plex Mono (SIL OFL)

Script:
  {"hook": "STOP BABYSITTING YOUR AI AGENT",          # sticker on the first shot
   "shots": [{"say": "Stop alt-tabbing to babysit your agent.", "clip": "agent", "from": 0.0,
              "show": "optional sticker text for this shot"}, ...],
   "cta": "Link in bio",                                # sticker on the last shot
   "caption": "post caption"}
The older {"hook", "beats": [{"text", "clip", "secs"}], "cta"} format still works.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

from workforce.policies.engine import IntegrationError

from .voice import duration, plain

ASSETS = Path(__file__).resolve().parents[2] / "assets"
W, H, FPS, XD = 1080, 1920, 30, 0.3
INK, BONE, AMBER = "0x0d0f13", "0xf6f2ea", "0xffb347"
HOOK_CLIP, CTA_CLIP = "hover", "tock-task-done"
SITE = "cue.mithunsidhaarth.in"
# per-clip framing before scaling: Tock's cards are cropped to the notch and the creature
CROP = {"tock": "crop=1080:720:0:20,", "home": "", "media": "crop=760:600:160:0,", "_": "crop=860:700:110:0,"}
TRANSITIONS = ["smoothup", "zoomin", "slideleft", "circleopen", "smoothleft", "fadefast"]


def clips(assets: Path = ASSETS) -> list[str]:
    return sorted(p.stem for p in (assets / "cue" / "clips").glob("*.mp4"))


def normalize(script: dict) -> dict:
    """Old beats format -> shots."""
    if script.get("shots"):
        return script
    shots = [{"say": script.get("hook", ""), "clip": script.get("hook_clip") or HOOK_CLIP}]
    shots += [{"say": b.get("text", ""), "clip": b.get("clip"), "from": b.get("from", 0)} for b in script.get("beats") or []]
    shots += [{"say": script.get("cta") or "Link in bio.", "clip": CTA_CLIP}]
    return {**script, "shots": shots, "cta": "LINK IN BIO"}


def check_script(script: dict, assets: Path = ASSETS) -> list[str]:
    s = normalize(script)
    have, problems = set(clips(assets)), []
    if not str(s.get("hook") or "").strip():
        problems.append("no hook")
    elif len(str(s["hook"])) > 48:
        problems.append("hook longer than 48 characters")
    shots = s.get("shots") or []
    if not 3 <= len(shots) <= 8:
        problems.append("needs 3-8 shots")
    for sh in shots:
        if sh.get("clip") not in have:
            problems.append(f"unknown clip {sh.get('clip')!r}")
        if not plain(str(sh.get("say") or "")):
            problems.append("a shot has nothing to say")
        elif len(plain(sh["say"])) > 120:
            problems.append(f"line too long: {sh['say'][:40]!r}")
    words = sum(len(plain(str(sh.get("say") or "")).split()) for sh in shots)
    if not 12 <= words <= 90:
        problems.append(f"{words} spoken words (keep 12-90, about 6-35 s)")
    return problems


def _ff(args: list[str], timeout: float = 300) -> None:
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise IntegrationError(f"ffmpeg failed: {p.stderr[-500:]}")


def chunks(text: str, max_words: int = 3, max_chars: int = 15) -> list[str]:
    """Caption chunks: up to 3 words / 15 characters, breaking after punctuation."""
    out, cur = [], []
    for w in plain(text).split():
        if cur and len(" ".join(cur + [w])) > max_chars:
            out.append(" ".join(cur))
            cur = []
        cur.append(w)
        if len(cur) >= max_words or re.search(r"[.,!?;:]$", w):
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def timings(parts: list[str], start: float, end: float) -> list[tuple[float, float]]:
    """Spread caption chunks over [start, end] in proportion to their length."""
    weights = [len(p) + 3 for p in parts]
    total, t, out = sum(weights), start, []
    for wgt in weights:
        d = (end - start) * wgt / total
        out.append((t, t + d))
        t += d
    return out


def _esc(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _text_file(tmp: Path, name: str, text: str) -> str:
    f = tmp / name
    f.write_text(text, encoding="utf-8")
    return _esc(f)


def _shot(tmp: Path, k: int, sh: dict, secs: float, speech: tuple[float, float], last: bool, assets: Path) -> Path:
    head, mono = _esc(assets / "fonts" / "Archivo-w1125-900.ttf"), _esc(assets / "fonts" / "IBMPlexMono-Regular.ttf")
    length = secs + (0 if last else XD)
    pre = CROP.get("tock" if sh["clip"].startswith("tock-") else sh["clip"], CROP["_"])
    f = [f"[0:v]{pre}fps={FPS},setsar=1,split[a][b]",
         # backdrop: the same footage, blurred and darkened to fill the 9:16 frame
         f"[a]scale=-2:{H},crop={W}:{H},boxblur=28:2,eq=brightness=-0.22:saturation=1.2[bg]",
         # foreground: sharp footage, slightly wider than the frame, with a slow push-in
         f"[b]scale=w='2*trunc(540*(1+0.05*t/{length:.2f}))':h=-2:eval=frame,crop='min(iw,{W})':'min(ih,1400)':(iw-ow)/2:0[fg]",
         "[bg][fg]overlay=(W-w)/2:150[v0]",
         "[v0][1:v]overlay=0:0[v1]"]
    draws = []
    sticker = sh.get("show")
    if sticker:
        lines = textwrap.wrap(str(sticker).upper(), 14)[:3]
        y0 = 1230 - (len(lines) * 112) // 2
        for n, line in enumerate(lines):
            draws.append(f"drawtext=fontfile='{head}':textfile='{_text_file(tmp, f'h{k}_{n}.txt', line)}':expansion=none:"
                         f"fontsize=96:fontcolor={INK}:box=1:boxcolor={AMBER}:boxborderw=22:x=(w-text_w)/2:y={y0 + n * 112}:"
                         f"alpha='min(1,t/0.12)'")
    parts = chunks(sh.get("say", ""))
    for n, ((a, b), part) in enumerate(zip(timings(parts, *speech), parts)):
        color = AMBER if n == len(parts) - 1 else BONE
        draws.append(f"drawtext=fontfile='{head}':textfile='{_text_file(tmp, f'c{k}_{n}.txt', part.upper())}':expansion=none:"
                     f"fontsize=74:fontcolor={color}:borderw=8:bordercolor=black@0.85:x=(w-text_w)/2:"
                     f"y=1560-8*max(0\\,1-(t-{a:.2f})/0.12):enable='between(t,{a:.2f},{b:.2f})'")
    draws.append(f"drawtext=fontfile='{mono}':textfile='{_text_file(tmp, 'site.txt', SITE)}':expansion=none:"
                 f"fontsize=34:fontcolor={AMBER}:x=(w-text_w)/2:y=1828")
    f.append("[v1]" + ",".join(draws) + ",format=yuv420p[v]")
    out = tmp / f"shot{k}.mp4"
    _ff(["-ss", f"{float(sh.get('from') or 0):.2f}", "-stream_loop", "-1", "-t", f"{length:.2f}",
         "-i", str(assets / "cue" / "clips" / f"{sh['clip']}.mp4"), "-loop", "1", "-t", f"{length:.2f}", "-i", str(tmp / "shade.png"),
         "-filter_complex", ";".join(f), "-map", "[v]", "-t", f"{length:.2f}", "-r", str(FPS),
         "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-an", str(out)])
    return out


def render_cue_reel(script: dict, out_path: str | Path, assets: Path = ASSETS, voice=None) -> dict:
    """Render a reel. `voice` is a provider from integrations.video.voice (or None for captions only)."""
    problems = check_script(script, assets)
    if problems:
        raise IntegrationError("script can't be rendered: " + "; ".join(problems))
    s = normalize(script)
    shots = [dict(sh) for sh in s["shots"]]
    shots[0].setdefault("show", s["hook"])
    if s.get("cta"):
        shots[-1].setdefault("show", s["cta"])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # top and bottom shade so the notch and the captions always read
        _ff(["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:d=1", "-vf",
             "format=rgba,geq=r=0:g=0:b=0:a='255*(0.45*max(0\\,1-Y/220)+0.85*max(0\\,(Y-1250)/670))'",
             "-frames:v", "1", str(tmp / "shade.png")])
        durs, speech, vo = [], [], []
        for k, sh in enumerate(shots):
            if voice is not None:
                a = voice.say(sh["say"], tmp / f"vo{k}.mp3")
                d = duration(a)
                vo.append(a)
                speech.append((0.12, 0.12 + d))
                durs.append(max(1.4, d + 0.32))
            else:
                d = max(1.6, len(plain(sh["say"]).split()) / 2.8 + 0.5)
                speech.append((0.1, d - 0.15))
                durs.append(d)
        segs = [_shot(tmp, k, sh, durs[k], speech[k], k == len(shots) - 1, assets) for k, sh in enumerate(shots)]
        total = sum(durs)
        # video: chain xfade transitions; shot k starts at the sum of the earlier shots
        ins = sum((["-i", str(p)] for p in segs), [])
        if len(segs) == 1:
            graph, last = "[0:v]null[vx]", "vx"
        else:
            graph, prev, t = [], "0:v", 0.0
            for k in range(1, len(segs)):
                t += durs[k - 1]
                graph.append(f"[{prev}][{k}:v]xfade=transition={TRANSITIONS[(k - 1) % len(TRANSITIONS)]}:duration={XD}:offset={t:.3f}[x{k}]")
                prev = f"x{k}"
            graph, last = ";".join(graph), prev
        # audio: voice lines, a whoosh on each cut, a tick per caption, riser in, chime out
        starts = [sum(durs[:k]) for k in range(len(shots))]
        sfx = [("riser_1", 0.0, 0.35), ("impact_small_1", 0.05, 0.6)]
        sfx += [("whoosh_soft_1", max(0.0, t0 - 0.1), 0.55) for t0 in starts[1:]]
        for k, sh in enumerate(shots):
            parts = chunks(sh["say"])
            sfx += [("ui_tick_1", starts[k] + a, 0.12) for a, _ in timings(parts, *speech[k])]
        sfx.append(("confirm_chime_1", starts[-1] + 0.2, 0.6))
        a_in, a_chain, n = [], [], len(segs)
        for k, path in enumerate(vo):
            a_in += ["-i", str(path)]
            ms = int((starts[k] + speech[k][0]) * 1000)
            a_chain.append(f"[{n + k}:a]volume=1.0,adelay={ms}|{ms}[vo{k}]")
        base = n + len(vo)
        for j, (name, at, vol) in enumerate(sfx):
            a_in += ["-i", str(assets / "sfx" / f"{name}.mp3")]
            a_chain.append(f"[{base + j}:a]volume={vol},adelay={int(at * 1000)}|{int(at * 1000)}[s{j}]")
        labels = "".join(f"[vo{k}]" for k in range(len(vo))) + "".join(f"[s{j}]" for j in range(len(sfx)))
        a_graph = ";".join(a_chain) + f";{labels}amix=inputs={len(vo) + len(sfx)}:normalize=0,apad,atrim=0:{total:.2f}," \
            f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st={total - 0.5:.2f}:d=0.5[a]"
        _ff([*ins, *a_in, "-filter_complex", graph + ";" + a_graph, "-map", f"[{last}]", "-map", "[a]",
             "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-movflags", "+faststart", str(out_path)], timeout=600)
    from .render import validate_file
    return {"path": str(out_path), "voice": getattr(voice, "name", None), **validate_file(out_path)}
