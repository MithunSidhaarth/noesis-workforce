# Assets used by the reel maker (integrations/video/cue_reel.py)

| Folder | What | Where it came from | Licence |
|---|---|---|---|
| `cue/clips/` | 1080x1080 screen recordings of Cue's design prototype (`design/prototype.html`, `design/cue.html`) and Tock (`design/mascots.html`) | Mithun's own cue-notch repo, recorded headless | Mithun's own work |
| `cue/scripts.yaml` | starter reel scripts and captions | written for NOESIS from the Cue README | — |
| `sfx/` | ten sound effects | Motion-as-code kit (Motion_as_kit.zip) supplied by Mithun | supplied by the owner for his own posts |
| `fonts/` | Archivo (Black Expanded, Bold), IBM Plex Mono | Motion-as-code kit (upstream: Google Fonts / IBM) | SIL Open Font License 1.1 |

The visual style (ink background, bone text, signal-amber accents) follows the
pdoom-video engine by Giacomo Magnanini (MIT).
To add footage, drop another 1080x1080 H.264 clip into `cue/clips/`; the script
writer can use it by its file name.
