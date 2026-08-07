# Sekiclip — product roadmap

**Current release:** public beta `0.1.0-beta.2` · offline GUI + CLI · MIT  

**North star:** Turn a pile of clips, stills, and music into a **finished story** in one honest cut—with sound, words, and looks that feel intentional. Not a multi-track studio NLE.

Related: [LIMITS.md](LIMITS.md) · [PRIVACY.md](PRIVACY.md) · [ROADMAP_PREVIEW_TIMELINE.md](ROADMAP_PREVIEW_TIMELINE.md) · [SHIPPING.md](SHIPPING.md)

---

## Principles

- **Free forever** — no paid plan, no watermarks, no locked export quality.  
- **Offline** — media stays on the machine; no cloud accounts required.  
- **One cut, many looks** — In/Out + export pipeline stay authoritative; effects must map to export.  
- **Preview may be lighter than export** — by design for speed; final quality is the exported file.  
- **Say what ships** — optional hardware (GPU, etc.) falls back when unavailable.  
- **Honest limits** — not multi-track timeline, not broadcast certification, not stream download/DRM.

---

## Shipped (beta.2 + film pass)

| Area | Notes |
|------|--------|
| Preview + range timeline (In / playhead / Out) | Play → Out · Loop cut · scrub |
| Dual A/V fades · crop · volume · speed · flip | One-pass where possible |
| Logo overlay · burn SRT · GIF/WebP | |
| Export quality (resolution + audio kbps) | Original / 4K / 1080p / … |
| `render_cut` one-pass edit | CLI + GUI |
| **Color looks** (warm/cool/doc/night/soft film/HC/bw/sepia) + strength | Export + GUI Edit · CLI |
| **VFX presets** (vignette/grain/soft/sharpen/bloom) + strength | Export + GUI · CLI |
| **Titles / subtitle / lower-third / top** | drawtext on cut |
| **Music bed** + fades + optional duck | Separate audio under picture |
| **End card text** (last N seconds) | |
| **`assemble` CLI** — multi-clip xfade (crossfade, dip, wipe, …) | Hard cut fallback |
| Convert · compress · trim · batch · cancel | |
| Session save/load · recent files · undo/redo | Film fields in session |
| Large-file path (PCM audio, frame cache, optional proxy) | |
| Portable prefs · first-run tips · opt-in updates | |
| Windows Setup wizard · portable zip | End-user packaging |

**Honest gaps still open:** live preview of color/VFX/titles (export is truth); multi-font picker; B-roll track; project bin GUI; per-join transition in UI; platform preset picker in GUI.

---

## Roadmap overview

| Phase | Theme | Goal for makers | Status |
|-------|--------|-----------------|--------|
| **A** | Story + sound | Words and music make the cut *feel* like a film | **mostly done** (export) |
| **B** | Picture language | B-roll, color, simple VFX & transitions between shots | **partial** |
| **C** | Assemble a film | Bin → order → one story, markers, platform export | **CLI assemble only** |
| **D** | Pro polish | Stabilize, J/L-cut, confidence at export | later |
| **E** | Craft depth | Richer effects/presets without becoming an NLE | later |

Status keys: `planned` · `next` · `later` · `done` · `partial`

---

## Phase A — Story + sound · `partial` → export done

Empower emotion with **titles** and a real **music bed**.

| ID | Feature | Notes | Status |
|----|---------|--------|--------|
| A.1 | **Titles & lower-thirds** | Title / subtitle; center · lower-third · top; box | **done** (export; default font) |
| A.2 | **Music / voice bed** | Import separate audio under the picture | **done** |
| A.3 | **Music fades** | Independent fade-in/out for bed | **done** |
| A.4 | **Simple ducking** | `sidechaincompress` under dialogue | **done** (best-effort) |
| A.5 | **End card / CTA plate** | Text hold in last N seconds | **done** (text; logo end plate later) |

**Next polish:** font picker, timed title In/Out, live preview of titles/music meter.

**Exit:** A maker can open clips + music, add a title and end card, and export a watchable short film. ✅ (export path)

---

## Phase B — Picture language · `partial`

### B1 — Overlay & color

| ID | Feature | Notes | Status |
|----|---------|--------|--------|
| B.1 | **One B-roll / overlay track** | Second clip or still over the main cut | planned |
| B.2 | **Color looks** | Warm, Cool, Documentary, Night, Soft Film, High Contrast, B&W, Sepia + strength | **done** |
| B.3 | **Safe-area / vertical guide** | Preview guides for 16:9 and 9:16 | planned |

### B2 — Transitions (between assembled shots)

Transitions apply at **shot boundaries** via `sekiclip assemble` (CLI). GUI bin still planned.

| ID | Transition | Notes | Status |
|----|------------|--------|--------|
| B.4 | **Crossfade / dissolve** | Default soft join | **done** (CLI) |
| B.5 | **Dip to black / dip to white** | Classic punctuation | **done** (CLI) |
| B.6 | **Fade through color** | Short hold on brand color | later |
| B.7 | **Cut (hard)** | Explicit hard concat | **done** |
| B.8 | **Simple wipe / slide** | L/R + smooth/circle/pixelize family | **done** (CLI) |
| B.9 | **Zoom through / push** | Mild scale-based | later |

**Rule:** Every transition has a duration and uses the same filter graph on export.

### B3 — Simple visual effects (looks on the cut, not a compositing suite)

| ID | Effect family | Examples | Status |
|----|---------------|----------|--------|
| B.10 | **Film / texture** | Grain, vignette, soft bloom | **done** |
| B.11 | **Focus / clarity** | Soften, sharpen | **done** |
| B.12 | **Stylize (light)** | B&W, sepia (via color looks) | **done** |
| B.13 | **Motion** | Ken Burns on stills, freeze | planned |
| B.14 | **Speed ramps (simple)** | Ease-in/out speed | planned |
| B.15 | **Flash / pulse** | White flash at marker | planned |

**Out of effects scope (for now):** particle systems, 3D titles, tracking masks, marketplace effect packs, generative AI filters.

**Exit:** A multi-shot assemble can dissolve between clips, hold a look, and overlay one B-roll plate. (assemble + looks yes · B-roll no)

---

## Phase C — Assemble a film · `partial` (CLI)

| ID | Feature | Notes | Status |
|----|---------|--------|--------|
| C.1 | **Project bin** | Clips, stills, audio in one session list | planned |
| C.2 | **Assemble order** | CLI: ordered inputs → one file | **CLI done** |
| C.3 | **Per-shot transition pick** | One transition for whole assemble | **CLI done** (uniform) |
| C.4 | **Markers** | Named markers; jump; chapter list | planned |
| C.5 | **Platform export presets** | YouTube 1080p, Vertical 9:16, Square, “chat/share” size |
| C.6 | **Named versions** | Save cut + looks as v1/v2; restore |

**Exit:** Import a shoot day → order shots → transitions → music → title → export for a platform.

---

## Phase D — Pro polish · `later`

| ID | Feature | Notes |
|----|---------|--------|
| D.1 | **Stabilize** | Export-quality stabilize; preview can stay light |
| D.2 | **Denoise / gentle cleanup** | One strength slider |
| D.3 | **J-cut / L-cut** | Offset bed or main audio vs picture by ±N seconds |
| D.4 | **Export checklist** | Offline tips: loudness, missing title, long fades, no end card |
| D.5 | **Proxy UX finish** | Clear “proxy vs full” state; trust badge when looks match export |
| D.6 | **Loudness target** | Simple “speech” / “music” target (EBU-ish, best-effort) |

**Exit:** Rough phone footage becomes a confident, shareable master.

---

## Phase E — Craft depth · `later`

Only after A–C feel solid.

| ID | Feature | Notes |
|----|---------|--------|
| E.1 | **Effect stack (short)** | Up to 2–3 looks/effects ordered; still not a node graph |
| E.2 | **Transition gallery polish** | Preview strip of transition types at current join |
| E.3 | **Caption style packs** | High-contrast / soft / highlight word (on top of burn-in SRT) |
| E.4 | **Still-to-motion presets** | Push-in, pull-out, pan L/R on images |
| E.5 | **Picture-in-picture layout presets** | Corner, split, side-by-side (still one overlay) |

---

## Transitions & effects — product rules

1. **Few, excellent defaults** — better 8 transitions and 12 effects than 200 weak ones.  
2. **Export is truth** — no “looks cool in preview only.”  
3. **Duration & strength** — every item has at least one continuous control.  
4. **Undoable** — fits existing undo stack (session looks).  
5. **Offline filters** — ffmpeg / local libs; no account, no upload.  
6. **Performance** — heavy effects may be export-only or proxy-quality in preview (labeled).

---

## Suggested build order

1. **A.1 Titles** + **A.2–A.3 Music bed & fades**  
2. **B.4–B.7 Core transitions** (hard, crossfade, dip black/white)  
3. **B.2 Color looks** + **B.10–B.12 Light VFX**  
4. **C.1–C.3 Bin + assemble + per-shot transition**  
5. **B.1 Overlay B-roll**  
6. **C.5 Platform presets** + **D.4 Export checklist**  
7. **D.1 Stabilize** · **D.3 J/L-cut** · remaining polish  

---

## Explicitly out of scope

| Avoid | Why |
|-------|-----|
| Full multi-track NLE | Wrong product shape |
| Cloud AI auto-edit / generative video | Offline, free, private |
| Effect marketplaces / paid packs | Free forever |
| Stream download, DRM strip | Legal / ethics |
| Broadcast QC / ACES pipeline | Honesty ([LIMITS.md](LIMITS.md)) |

Code signing remains optional (maintainer cert); not a product “feature tier.”

---

## Success metrics (qualitative)

A first-time maker can, without a tutorial wall:

1. Drop footage + music  
2. Order a few shots and pick dissolves  
3. Add a title and end card  
4. Apply one look and one light effect  
5. Export a 1080p film that feels *finished*  

---

## Document history

| Version | Note |
|---------|------|
| beta.2 | Shipped baseline documented; roadmap expanded for story, sound, transitions, VFX |
