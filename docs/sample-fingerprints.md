# Sample fingerprint library — design sketch

**Status:** Not implemented. This doc outlines the approach so a future session
can build it without re-discovering the shape.

## Why

The agent currently treats every project as an island. If you used the same
kick sample in 20 projects, the agent re-learns its character (sub-heavy 40Hz,
tight 4kHz click) every time. A fingerprint library lets the agent recognize
"that's the same sample I shaped in Project_1woww three months ago; user kept
the 320Hz cut" and transfer priors.

This is distinct from:

- **`decisions.json`** — per-project, per-action history.
- **`user.json`** — cross-project *user* preferences.
- **Sample library** — cross-project *sample* identity + aggregated decisions
  across every appearance of that sample.

## Scope

Identify samples by audio content, not filename. The same one-shot can appear
as `kick.wav`, `Kick_808_Trap.wav`, `snare dirty 3.wav`, etc. across projects
and in different sample packs.

## What to use

**chromaprint** (via pyacoustid or fpcalc binary) — industry standard for
audio fingerprinting, fast, tolerant to pitch/tempo shifts, small fingerprint
size (~200 bytes per minute of audio). Originally built for music recognition
(Shazam-class problem) but works on single-hit samples too.

Alternative for single-hits: custom fingerprint from FFT peak structure +
transient shape. Lighter dependency but we'd be building what chromaprint
already does.

**Default: chromaprint.** Reconsider only if the dependency story gets ugly.

## Storage

`~/StudioMind/samples/fingerprints.json`:

```json
{
  "samples": [
    {
      "id": "sample_<hash>",
      "fingerprint": "AQAAA...",
      "first_seen": 1682...,
      "last_seen": 1685...,
      "appearances": [
        {
          "project": "project_1woww",
          "track_id": 4,
          "track_name": "Kick 1",
          "file_name": "Kick_Analog_3.wav",
          "first_seen": 1682...
        }
      ],
      "aggregated_spectral_balance": {
        "sub": -8.2, "low": -3.1, "low_mid": -12.0, ...
      },
      "decisions_across_projects": [
        {"project": "project_1woww", "tool": "set_builtin_eq", "outcome": "kept", ...}
      ]
    }
  ]
}
```

Size budget: at 256KB per 100 samples × 10 projects, that's comfortably under
2.5MB for most users.

## Integration points

1. **Fingerprint on analysis.** When `analyze_audio` runs on a stem, also
   compute the fingerprint (chromaprint). Add `sample_id` to the AudioAnalysis
   dict.

2. **Match against library.** On fingerprint computation, do an approximate
   match against existing library entries (chromaprint comparison is O(n) in
   the fingerprint length, O(k) in the library size — fine up to a few
   thousand samples). Threshold at ~95% similarity.

3. **New tool `read_sample_history(sample_id)`** — returns every past
   appearance of this sample with the decisions applied to it. Agent calls
   this in the Diagnose step if it spots a recurring sample that behaved
   predictably last time.

4. **Auto-aggregate.** After a session ends, run a cleanup pass that updates
   each sample's `aggregated_spectral_balance` and pulls in newly-kept
   decisions. Lazy; not time-critical.

## Failure modes to design around

- **Fingerprint collision:** two genuinely different samples fingerprint
  identically. Mitigation: secondary check on duration + peak count before
  declaring a match. Log mismatches so we can tune the threshold.
- **False non-match:** same sample at 44.1kHz vs 48kHz, same sample with
  slight time-stretch. Chromaprint is largely robust here but not perfect.
  If the user says "this is the same sample as last time" and the match
  failed, expose a `force_same_sample` tool so the agent can merge two
  fingerprint entries.
- **Storage blowup on busy users:** prune old samples with zero recent
  appearances after 90 days (keep their decisions attached to project logs,
  drop the fingerprint bytes). Not urgent — 2MB is nothing.

## What this is NOT

- Not a sample *playback* system. We don't host samples, we just recognize
  them by content.
- Not a sample *recommendation* system. "You used a similar kick here, try
  this one" is interesting but far out of scope.
- Not a content-ID system. We match samples the user has already handed us;
  we don't try to identify commercial releases.

## Effort estimate

- Day 1: chromaprint integration (pyacoustid wheel story, fallback to fpcalc
  binary on platforms with no wheel), fingerprint on analyze_audio.
- Day 2: fingerprint library storage + matching + dedupe.
- Day 3: `read_sample_history` tool, prompt integration, test on a project
  with known repeated samples across sessions.
- Day 4: aggregation pass + prune old entries.

Call it a week of focused work to ship, not counting the inevitable "wait,
chromaprint doesn't match my kick sample because FL's export pitch-shifted
it by one cent" debugging.

## Prerequisites

Should ship *after* the vertical-slice test passes (docs/vertical-slice-test.md)
and the Phase 2 compression/reverb/dynamics tools land. Without those, the
agent can recognize the sample but has nothing interesting to do with the
recognition. Premature.
