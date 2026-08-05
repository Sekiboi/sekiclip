# Clipwork — limits & honesty

## What it is

Offline **file toolkit** for common convert / compress / trim jobs. Not a full creative suite.

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
