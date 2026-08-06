# Clipwork — limits & honesty

## What it is

Offline **visual media editor** for common convert / compress / trim jobs: preview, timeline scrub, In/Out, export progress.

Not a multi-track NLE, color grading suite, or DAW.

## Preview

- **Video:** OpenCV sequential playback (clock-synced, drops frames if behind). Scrub seeks by frame index. Preview stage is letterboxed 16:9.
- **Audio:** Preview sound via **ffplay** (same family as ffmpeg). Needs `ffplay` on PATH (full ffmpeg builds include it).
- **Audio-only:** Waveform overview + audible play through ffplay.
- **Image:** Still preview.
- Preview is for timing decisions; final quality is the exported file.
- A/V sync is best-effort (separate video clock + ffplay audio).

## ffmpeg

- Video and audio operations need **ffmpeg** and **ffprobe**.
- Image ops use **Pillow** only (no ffmpeg).
- Trim with stream copy may cut on **keyframes**; use **re-encode** for tighter accuracy.
- Concat re-encodes to a common size (1280×720) for reliability; quality is not archival-grade.

## Quality

- Compress presets favor small files and chat/email use cases.
- Loudnorm is single-pass (good enough, not mastering).
- No GPU encode matrix in v1 (optional later).

## Not included (by design)

- Multi-track timeline / effects / color grading
- Subtitle authoring
- Device capture / streaming
- Cloud accounts
- OCR or AI enhance APIs
- Guaranteed frame-perfect pro edit workflows

## Privacy

Nothing is uploaded. Job logs store **basenames only**.
