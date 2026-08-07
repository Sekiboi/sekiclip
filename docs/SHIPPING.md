# Shipping Sekiclip (maintainers only)

End users get **`Sekiclip-<ver>-Setup.exe`** — a normal Windows wizard.  
They should never need Python, git, or this repo.

## Build the end-user release

Requirements on the **build PC**:

- Python 3.10+  
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`)  
- Network once (to fetch ffmpeg essentials), or pre-drop binaries in `vendor\`

```powershell
.\scripts\package_release.ps1
```

### What it does

1. Ensures Inno Setup (`scripts\ensure_inno.ps1`)  
2. Downloads ffmpeg into `vendor\` if missing  
3. PyInstaller → `dist\Sekiclip\`  
4. Compiles wizard → **`dist\Sekiclip-<ver>-Setup.exe`**  
5. Optional portable zip + `SHA256SUMS.txt`  

### Ship to testers

| File | Audience |
|------|----------|
| **`Sekiclip-…-Setup.exe`** | **Everyone** — primary |
| `Sekiclip-…-portable.zip` | Optional USB / no-install |
| `SHA256SUMS.txt` | Optional integrity check |

Do **not** ship the source tree, `run.py`, or “install Python” instructions as the product.

## End-user layout (what Setup installs)

| | Path |
|--|------|
| App | `%LOCALAPPDATA%\Programs\Sekiclip` |
| Settings / logs / proxy | `%LOCALAPPDATA%\Sekiclip` |
| Shortcuts | Start Menu (+ desktop if chosen) |
| Uninstall | Start Menu + Settings → Apps |

No admin. Upgrade = run Setup again. Uninstall keeps settings unless the user deletes the data folder.

## Signing

Not automated. Sign `Setup.exe` and/or `Sekiclip.exe` with your certificate for fewer SmartScreen warnings.

## Dev loop (not for end users)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
pytest -q
```
