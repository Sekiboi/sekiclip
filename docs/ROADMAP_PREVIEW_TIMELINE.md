# Preview, timeline & performance

Goal: smooth scrubbing and play, fades tied to **In/Out**, large files stay usable, **export quality unchanged**. Realistic limits apply (see [LIMITS.md](LIMITS.md)).

## Status

| Theme | Status |
|-------|--------|
| Scrub UX, lag, fades on current In/Out | Done |
| Coherent play clock + session integrity | Done |
| Preview architecture (audio engine, async frames, optional hwaccel) | Done |

## Timeline & scrub

- Click / drag on track → move **playhead only**
- Drag green / red handles → In / Out only
- Move whole range via **Alt+drag** on selection
- Hold-drag is first-class; double-click force-refreshes
- During drag: **video only**, throttled paint/seek
- On mouse-up: one accurate frame finalize; optional short audio blip
- No full A/V restart on every motion event

## Fades

- Fade-in = first N **output** seconds after In; fade-out = last N before Out
- Outside selection: dim + badge
- In/Out drag updates preview immediately (throttled)

## Play clock

- **Master** = audio sample clock when the PCM engine is active; otherwise wall clock
- Video follows the master; scrub pauses audio; Play starts A+V from the same time
- Labels: **Play → Out** (Space) vs **Loop cut** (In→Out loop)

## Session integrity

- Clamp invalid In/Out/playhead
- Detect dead OpenCV capture; try re-open
- Resync transport and time labels when idle
- Clean leftover staging files when idle

## Preview vs export

| | Preview | Export |
|--|---------|--------|
| Decode | Downscaled + async frame cache | Full source → cut / stream copy |
| Seek | Fast / approximate OK | Accurate cut + filters |
| Audio | PCM engine (ffplay fallback) | Exact bitrate from UI |
| Quality | Display only | User resolution / kbps / Original |

Optional **scrub proxy** builds a low-res local cache; export still uses the original.

## Non-goals

- No artificial max file size
- No proxy re-encode for every scrub
- No “fast seek” approximations on **export**

## Related

- [LIMITS.md](LIMITS.md) — large files, preview vs export quality  
- `sekiclip/preview/match.py` — cut timeline + fade math  
- `sekiclip/preview/timeline_widget.py` — range UI  
- `sekiclip/preview/session.py` — session seek/play/audio  
