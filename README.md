# Sekiclip

Offline media editor for **Windows**. Preview, set In/Out, export.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Offline](https://img.shields.io/badge/privacy-100%25%20offline-brightgreen)
![Beta](https://img.shields.io/badge/status-public%20beta-orange)

<p align="center">
  <img src="assets/sekiclip.png" width="96" height="96" alt="Sekiclip">
</p>

**Public beta** `0.1.0-beta.2` · Free forever (MIT)

---

## Install (Windows)

1. Download **`Sekiclip-…-Setup.exe`** from the [latest release](https://github.com/Sekiboi/sekiclip/releases/latest)
2. Run the installer (Next → Install → Finish)
3. Sekiclip opens — drag a file in

No Python. No admin password. Video tools are included.

| On your PC | Where |
|------------|--------|
| **App** | `%LOCALAPPDATA%\Programs\Sekiclip` |
| **Settings** | `%LOCALAPPDATA%\Sekiclip` (kept if you uninstall) |

**Uninstall:** Start Menu → Sekiclip → Uninstall, or Settings → Apps → Sekiclip  

If Windows says **“Windows protected your PC”** (unsigned beta): More info → Run anyway.

**Portable (optional):** download the portable zip and run `Sekiclip.exe` from the folder.

---

## What you can do

- Timeline In/Out · Play → Out · Loop cut  
- Export quality (resolution + audio kbps)  
- Fades, crop, volume, speed, GIF, logo, burn SRT  
- **Film looks:** color presets, light VFX, titles, end card, music bed (+ duck)  
- **Assemble** (CLI): join clips with crossfade / dip / wipe  
- Convert, compress, extract audio, images, batch  

Preview may look softer than export. **Final quality is the exported file.**  
Not a multi-track NLE — [limits](docs/LIMITS.md) · [privacy](docs/PRIVACY.md) · [roadmap](docs/ROADMAP_PRODUCT.md)

```bash
# Example: one-pass film cut
python -m sekiclip render-cut clip.mp4 -o out.mp4 --color-look warm --vfx vignette \
  --title "Open" --end-card "Thanks" --music bed.mp3 --music-duck

# Join shots with transitions
python -m sekiclip assemble a.mp4 b.mp4 c.mp4 -o film.mp4 --transition crossfade
```
