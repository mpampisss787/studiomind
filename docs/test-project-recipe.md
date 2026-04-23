# StudioMind Test Project — build recipe

A minimal FL Studio project with **intentional mixing problems** so StudioMind
has something real to diagnose and fix. Stock plugins only, ~10-15 min to
build. Use this as the canonical test bed for the vertical-slice test
(`docs/vertical-slice-test.md`) and any future regression checks.

## Why a hand-built project and not a preset / demo

- FL's bundled demos use third-party plugins and have pristine mixes — nothing
  for StudioMind to improve.
- The problems below are **specific and measurable**, so you can verify
  StudioMind spotted what it should have spotted.
- Building it yourself means you know exactly what's there.

## Project settings

| Setting | Value |
|---------|-------|
| **File name** | `StudioMind_Test_Beat.flp` |
| **Save to** | Your usual FL Projects folder (e.g. `Documents/Image-Line/FL Studio/Projects/StudioMind_Test_Beat/`) |
| **BPM** | `120` |
| **Length** | 8 bars, looped |
| **Time signature** | 4/4 |
| **Sample rate** | Whatever FL is on (44.1 or 48 kHz both fine) |

## Channels (Channel Rack — 6 instruments, stock only)

| # | Instrument | Source | Notes |
|---|-----------|--------|-------|
| 1 | **Kick** | FPC → `TR-909 Kick` (or any stock kick sample) | Default settings |
| 2 | **Clap** | FPC → `TR-909 Clap` | Default |
| 3 | **Hi-Hat** | FPC → `TR-909 Hi-Hat Closed` | Default |
| 4 | **Bass** | BooBass (stock) | Default preset, no tweaks |
| 5 | **Chord** | FL Keys (stock) | Default electric-piano preset |
| 6 | **Lead** | 3× Osc (stock) | Load the default saw preset. Turn Osc 2 fine-tune to +10 and Osc 3 to -10 for thickness. |

Everything else at defaults. No EQ, no compression, no effects on any channel.

## Mixer routing

- Route each channel to its own mixer insert, in order:
  - Channel 1 (Kick) → Insert 1
  - Channel 2 (Clap) → Insert 2
  - Channel 3 (Hi-Hat) → Insert 3
  - Channel 4 (Bass) → Insert 4
  - Channel 5 (Chord) → Insert 5
  - Channel 6 (Lead) → Insert 6
- Leave all inserts at default volume (0 dB, ~0.78 fader position)
- **Master**: add **Fruity Limiter** on the Master insert. Default preset. (This will clip at the default ceiling — that's intentional.)
- **No EQ plugins anywhere**, no compression, no send effects. The built-in 3-band EQ on each track is enough for StudioMind to work with.

## Patterns

Four patterns, one instrument each.

### Pattern 1 — "Beat" (Kick + Clap + Hi-Hat via step sequencer)

Step sequencer (16 steps = 1 bar). Turn on:

```
Kick:    X . . . X . . . X . . . X . . .
Clap:    . . . . X . . . . . . . X . . .
Hi-Hat:  . . X . . . X . . . X . . . X .
```

Standard 4-on-the-floor with clap on 2 and 4, offbeat hats.

### Pattern 2 — "Bass" (Piano Roll on Bass channel)

Four whole-bar notes, one per bar, octave 2:

```
Bar 1: C2
Bar 2: C2
Bar 3: Ab1
Bar 4: G1
```

(Simple Cm / Ab / G progression root notes.)

### Pattern 3 — "Chords" (Piano Roll on Chord channel / FL Keys)

Held chords, 2 bars each:

```
Bars 1–2: Cm  (C3 + Eb3 + G3)
Bars 3–4: Ab  (Ab3 + C4 + Eb4)
Bars 5–6: Fm  (F3 + Ab3 + C4)
Bars 7–8: G   (G3 + B3 + D4)
```

Full velocity, sustain across each 2-bar block.

### Pattern 4 — "Lead" (Piano Roll on 3× Osc Lead channel)

Simple melody, 8th notes, pentatonic over the chord progression. Any melody
you like — the specifics don't matter for the test. One easy option:

```
Bars 3–4:  Eb4 G4 C5 G4 Eb4 G4 Bb4 G4
Bars 5–6:  Ab4 C5 Eb5 C5 Ab4 C5 Eb5 C5
Bars 7–8:  G4 Bb4 D5 Bb4 G4 Bb4 D5 Bb4
```

Starts at bar 3 so there's some variation (intro without the lead).

## Playlist

- Pattern 1 (Beat) — bars 1-8
- Pattern 2 (Bass) — bars 1-8
- Pattern 3 (Chords) — bars 1-8
- Pattern 4 (Lead) — bars 3-8

Loop from bar 1 to 8.

## Save

`File → Save As` → name it exactly `StudioMind_Test_Beat.flp`. FL will create
a matching folder. This name is what the FL window title will read as; the
project-detection code in `src/studiomind/fl_detect.py` parses the title, so
StudioMind's workspace will be at `~/StudioMind/projects/studiomind_test_beat/`.

## The intentional problems StudioMind should diagnose

When you run `/autorender and analyze`, StudioMind should be able to identify
most or all of these. Use this list to grade its output.

| # | Problem | Where | How to recognize in the analysis |
|---|---------|-------|-----------------------------------|
| 1 | **Master clipping** | Master output | `true_peak_db` at or very near 0.0 dB |
| 2 | **Low-end masking**: Kick + Bass overlap | Tracks 1 + 4 | Both tracks have strong `low` band (60-250 Hz) energy. Kick centroid ~80Hz, Bass centroid ~120Hz — they're stacked. |
| 3 | **Low-mid muddiness**: Chord + Bass stack 200-400 Hz | Tracks 4 + 5 | Chord (FL Keys) has no high-pass; its low harmonics pile on top of Bass. Both `low_mid` bands should be hot. |
| 4 | **Lead synth harshness**: raw saw with no EQ | Track 6 | `high_mid` (2-4 kHz) and `presence` (4-8 kHz) bands should both be +3 to +6 dB hotter than the rest. Centroid ~2.5-3 kHz. |
| 5 | **Wide range on chord track**: no high-pass | Track 5 | Full-spectrum energy down to 150 Hz — FL Keys fundamentals. Should be high-passed to ~200Hz so the bass has room. |

## What a good StudioMind run looks like on this project

1. Agent orients — reads workspace status, history (empty), preferences.
2. Agent renders everything (initial analysis) — finds problems 1-5.
3. Agent proposes *one* fix — typically starting with either the master
   clipping (obvious headroom issue) or the kick/bass masking (most impactful
   sonic issue).
4. Agent states a predicted spectral delta.
5. Agent applies the change (`set_builtin_eq` on the relevant track).
6. Agent re-renders that track + master, reports the actual vs predicted delta.
7. User says "good, continue" or "revert". Repeat for the next problem.

If StudioMind flags fewer than 3 of the 5 issues on its initial pass, that's
a prompt regression worth investigating. If it flags all 5 and ranks them
sensibly, we're shipping.

## If you want a shorter version

For a faster smoke test, skip Pattern 4 (Lead) entirely — just 3 channels
(kick, bass, chords) on 3 inserts. Problems 1, 2, 3 still present; 4 and 5
disappear. Builds in ~5 minutes instead of 10-15.
