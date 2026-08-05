# Clipwork

Offline media toolkit for Windows. Convert, compress, trim, and fix common **video, audio, and image** files on your PC — **no accounts, no uploads, no paid tier**.

Requires **ffmpeg** on PATH (or under `vendor/`) for video and audio.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Offline](https://img.shields.io/badge/privacy-100%25%20offline-brightgreen)
![Beta](https://img.shields.io/badge/status-local%20beta-orange)

**Local beta** `0.1.0-beta.1` · Free forever (MIT) · Sister project to [Sekikit](https://github.com/Sekiboi/sekikit) (PDF)

---

## Features

| Area | Tools |
|------|--------|
| **Video** | Convert, compress presets, trim, remux, rotate, strip audio, frame grab, concat |
| **Audio** | Convert, compress, extract from video, normalize, mono |
| **Image** | Convert, resize, compress, rotate, flip, strip EXIF, images→PDF |
| **CLI** | Same toolkit from the command line |

This is a **file toolkit**, not a timeline editor (no multi-track NLE, effects suite, or DAW).

See [docs/LIMITS.md](docs/LIMITS.md).

---

## Requirements

- **Windows 10/11** (primary)
- **Python 3.10+** (for source)
- **ffmpeg** + **ffprobe** on PATH, or binaries in `vendor/`

---

## Run from source

```powershell
cd path\to\clipwork
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pythonw run.py
```

CLI:

```powershell
$env:PYTHONPATH = "."
python -m clipwork info video.mp4
python -m clipwork compress video.mp4 --preset chat
python -m clipwork trim video.mp4 --start 0 --end 5 --reencode
python -m clipwork extract-audio video.mp4 -f mp3
python -m clipwork resize photo.jpg --max-edge 1280
python -m clipwork --help
```

---

## Privacy

Clipwork does not phone home. Media stays on your machine unless you copy it.

Optional anonymous diagnostics (default **off**) only build a local text report you can copy. See [docs/PRIVACY.md](docs/PRIVACY.md).

---

## Develop

```powershell
pip install -r requirements.txt pytest
pytest -q
```

Not on GitHub yet — local project only until the first working beta is solid.

---

## License

[MIT](LICENSE) — free for personal and commercial use. **No paid edition.**
