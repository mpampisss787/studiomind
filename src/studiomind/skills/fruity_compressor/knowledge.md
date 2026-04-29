# Fruity Compressor

## Capabilities

Stock FL Studio dynamics processor. Six controls — threshold, ratio,
attack, release, makeup gain, knee — all calibrated against live FL
on 2026-04-29. Use `set_compressor` with human units; the wrapper
handles normalization.

## Use cases

- **Vocals (lead)** — threshold -18 to -12 dB, ratio 3:1 to 4:1,
  attack ~5 ms, release ~80 ms, gain to taste. Smooth knee for
  transparency, hard for punch.
- **Bass** — threshold -14 to -10 dB, ratio 4:1, attack ~10 ms,
  release ~120 ms. Pair with `apply_sidechain` to duck against kick.
- **Kick (parallel)** — threshold -10 dB, ratio 4:1, fast attack
  (~0.1 ms) for click, short release (~50 ms).
- **Master bus glue** — threshold -8 dB, ratio 2:1, attack ~30 ms,
  release ~150 ms, gain to make up the few dB the comp pulls down.

## Calibrated ranges (2026-04-29)

- THRESHOLD: linear `[-60, 0]` dB
- RATIO: linear, `ratio = 0.386 + 29.629 × param`, top out 30:1
- ATTACK: linear `[0, 400]` ms
- RELEASE: linear `[0, 4000]` ms
- GAIN: linear `[-30, +30]` dB, unity at param=0.5
- KNEE: hard / smooth

## Gotchas

- Sidechain-source dropdown is FL plugin-wrapper UI only — not a VST
  param. Use `apply_sidechain` to wire the audio routing; it returns
  an advisory with the one right-click step you do in FL.
- "Release" knob on this comp affects both the gain stage and the
  knee. If you need ultra-fast release, also shorten attack to keep
  the comp from "breathing."
- Hard-knee + low ratio (1.5:1) is good for "barely there" glue.
  Smooth-knee + higher ratio (4:1+) for transparent control.
