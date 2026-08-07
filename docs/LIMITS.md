# Sekiclip — limits

## What it is

Offline **visual media editor** for convert / compress / trim / light edit: preview, **range timeline** (In/Out), play selection, crop, cancelable export, batch, compress presets, volume/speed/GIF/fade/flip, size target, SRT burn-in, logo.

**Free forever** — no paid plan, no watermarks, no locked export quality. Optional hardware (GPU) is used only when available and falls back to software.

**Portable data:** set env `SEKICLIP_PORTABLE` to a folder, or place `sekiclip_portable.txt` next to the app (prefs go under `./data`).

Not a multi-track NLE, color suite, screen recorder, or stream downloader.

## Large files (size / length)

- **No artificial max file size.** Multi‑GB sources are supported; practical limits are your RAM, disk free space, and CPU time.
- **Export quality is independent of preview.** Preview may downscale decode for speed; the written file uses your chosen **Video quality** (Original / 4K / 1080p / …) and **Audio quality** (kbps).
- **Stream copy** (Trim without re-encode / looks) copies packets — no quality loss and very little extra disk use.
- **Re-encode** needs free disk roughly **1.5–2×** source size (staging + output). Sekiclip warns if free space looks tight; you can still continue.
- **ffmpeg** jobs use **all CPU cores** (`-threads 0`). Same CRF / bitrate → same quality, faster finish on multi-core machines.
- **Probe** reads only a header window (not the whole file), so open stays quick even for huge media.

## Preview vs export

| | Preview | Export |
|--|---------|--------|
| Decode | OpenCV (~1280px) + async ffmpeg frame cache (optional hwaccel) | Full source → cut / stream copy |
| Seek | Fast / approximate OK | Accurate cut + filters |
| Audio | Local PCM preview engine; ffplay fallback | Exact AAC bitrate from UI |
| Clock | Audio-master when PCM playing; else wall clock | N/A |
| Quality | Display only | User resolution / kbps / Original |

Deps: `sounddevice`, `numpy` (preview only).

**Optional scrub proxy:** builds a low-res local cache for smoother scrub; export still uses the original.  
**GPU encode:** tried only when enabled; otherwise or on failure uses CPU.

## Preview notes

- **Video:** sequential playback with wall-clock sync (drops frames if behind). Frames are **downscaled for display** so 4K/8K scrubbing stays responsive; export is full quality.
- **Audio:** primary path is the local PCM engine; **ffplay** is a fallback when that is unavailable.
- **Audio-only:** waveform overview (fast downsampled scan; long files generate in the background) + audible play.
- **Image:** still preview (large images thumbnail for display only).
- Preview is for timing decisions; final quality is the exported file.
- A/V sync is best-effort.

## ffmpeg

- Video and audio operations need **ffmpeg** and **ffprobe**.
- Image ops use **Pillow** only (no ffmpeg).
- Trim with stream copy may cut on **keyframes**; use **re-encode** for tighter accuracy.
- Concat re-encodes to a common size (1280×720) for reliability; quality is not archival-grade.

## Quality

- **Edit / Trim export:** pick **Video quality** (Original, 4K, 1080p recommended, 720p, 480p) and **Audio quality** (320 / 256 recommended / 192 / 128 kbps). Resolution caps never upscale smaller sources.
- Compress presets favor small files and chat/email use cases.
- Loudnorm is single-pass (good enough, not mastering).
- Optional GPU encode when hardware/drivers allow; otherwise software encode.

## Not included (by design)

- Multi-track timeline / effects / color grading
- Subtitle authoring
- Device capture / streaming
- Cloud accounts
- OCR or AI enhance APIs
- Guaranteed frame-perfect pro edit workflows

## Privacy

Nothing is uploaded. Job logs store **basenames only**.
