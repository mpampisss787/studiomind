# FabFilter Pro-Q 3

## Capabilities

Surgical and tonal-shaping EQ. Ten bands per instance, each
configurable as bell, low/high shelf, low/high cut, notch, band pass,
or tilt shelf. Hz/dB/Q knobs are converted automatically — call
`set_proq3` with human units, the wrapper handles normalization.

## Use cases

- **Surgical cuts** for resonances and masking — narrow Q (8–20+),
  -3 to -8 dB, identified via `find_resonances` first.
- **Broad tonal shaping** of a stem — wide Q (0.5–1.0), ±2 to ±4 dB,
  on a bell at the dominant frequency band.
- **High-pass / low-pass** to clean up sub-rumble or air — cut shapes
  at 12 or 24 dB/oct, frequency at the corner you want.
- **Tilt shelf** for "more brightness" / "more darkness" — single
  band, tilt_shelf shape, gentle slope, ±1.5 to ±3 dB.

## Gotchas

- Band 1 indexing in the API is **1-based** (1–10), not 0-based.
- Pro-Q 3 has Dynamic EQ per band but the v1 wrapper does not expose
  it. Use the static gain only.
- Slope only applies to cut and shelf shapes; bell ignores it.
- Param IDs are `band_index * 13 + offset_within_band` — the wrapper
  hides this; never hand-write the IDs.
