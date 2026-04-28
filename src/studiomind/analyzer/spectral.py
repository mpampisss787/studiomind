"""
Audio analysis: spectral profile, LUFS, true peak, masking detection,
plus STFT-derived dynamics, transients, resonances, and stereo-over-time.

The analyzer computes a windowed STFT once per file and derives every
summary field from it. The STFT magnitude matrix is exposed via
`analyze_audio_full()` so callers (notably the cache layer) can persist
it for drill-down tools without recomputing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

# Frequency band definitions (Hz)
BANDS: dict[str, tuple[float, float]] = {
    "sub": (20, 60),
    "low": (60, 250),
    "low_mid": (250, 500),
    "mid": (500, 2000),
    "high_mid": (2000, 4000),
    "presence": (4000, 8000),
    "air": (8000, 20000),
}

# Floor for log10 of zero or sub-noise energy. JSON can't encode -inf.
DB_FLOOR = -120.0


def band_energy_db(
    mag: np.ndarray,
    freqs: np.ndarray,
    f_low: float,
    f_high: float,
    *,
    floor_db: float = DB_FLOOR,
):
    """Sum squared magnitudes in `[f_low, f_high)` along the last axis, return dB.

    Scalar in / scalar out for 1-D `mag` (a whole-file or mean spectrum).
    `(N,)` out for 2-D `(N, n_bins)` `mag` (a per-frame STFT).

    Bands that contain no FFT bins or have zero summed energy return `floor_db`.
    """
    mask = (freqs >= f_low) & (freqs < f_high)
    if not bool(mask.any()):
        if mag.ndim == 1:
            return float(floor_db)
        return np.full(mag.shape[0], floor_db, dtype=np.float32)
    energy = np.sum(np.asarray(mag)[..., mask] ** 2, axis=-1)
    if mag.ndim == 1:
        e = float(energy)
        return float(10.0 * math.log10(e + 1e-10)) if e > 0 else float(floor_db)
    out = np.full(energy.shape, floor_db, dtype=np.float32)
    nz = energy > 0
    out[nz] = 10.0 * np.log10(energy[nz] + 1e-10)
    return out


@dataclass
class AudioAnalysis:
    """Result of analyzing an audio file."""

    path: str
    sample_rate: int
    duration_s: float
    channels: int
    lufs: float
    true_peak_db: float
    spectral_centroid_hz: float
    spectral_balance: dict[str, float]  # sub/low/.../air in dB
    rms_db: float
    # "ok" = audible content present, "silent" = readable but ~zero energy
    # (intentional silence, muted stem, or a stem that doesn't play in this
    # section). "broken" is unreachable here — unreadable files raise in
    # analyze_audio and are handled as failures upstream.
    status: str = "ok"

    # Stereo whole-file fields (None on mono).
    correlation: float | None = None
    side_ratio_db: float | None = None
    side_balance: dict[str, float] | None = None

    # ── STFT-derived summary fields ──
    # Peak-to-RMS in dB. High = punchy/dynamic, low = squashed.
    crest_factor_db: float | None = None
    # Loudness Range (LU): 95th - 10th percentile of short-term LUFS.
    # Approximates EBU R128 LRA. None when pyloudnorm is unavailable or the
    # file is shorter than one short-term window.
    lra_lu: float | None = None
    # Transients per second: rising edges where per-frame energy spikes >6 dB
    # above a rolling median. 0.0 for tonal/sustained content; 4–8 for kicks.
    transient_density_per_s: float | None = None
    # Top spectral peaks on the frame-averaged spectrum.
    # Each entry: {hz, db, q_est, prominence_db}.
    top_resonances: list[dict] | None = None
    # Minimum L/R correlation across STFT frames (None on mono).
    # Catches brief out-of-phase moments that whole-file correlation hides.
    correlation_min: float | None = None

    # ── Tonal fields (YIN-based) ──
    # Median fundamental across voiced frames. None for noise/percussion
    # / silence (voicing_ratio drops below threshold).
    fundamental_hz: float | None = None
    # Fraction of frames that voted as "voiced" by the YIN CMND test.
    # Useful as a tonality signal: ~0.0 for kicks/hats/noise,
    # 0.5–1.0 for bass / vocals / leads.
    voicing_ratio: float | None = None

    def summary(self) -> str:
        """Human-readable summary for the agent."""
        header = f"Audio: {Path(self.path).name}"
        if self.status == "silent":
            header += "  [silent — no audible content]"
        lines = [
            header,
            f"  Duration: {self.duration_s:.1f}s, {self.sample_rate}Hz, {self.channels}ch",
            f"  LUFS: {self.lufs:.1f}, True Peak: {self.true_peak_db:.1f} dB, RMS: {self.rms_db:.1f} dB",
            f"  Spectral centroid: {self.spectral_centroid_hz:.0f} Hz",
        ]
        if self.crest_factor_db is not None:
            lines.append(f"  Crest factor: {self.crest_factor_db:.1f} dB")
        if self.lra_lu is not None:
            lines.append(f"  LRA: {self.lra_lu:.1f} LU")
        if self.transient_density_per_s is not None:
            lines.append(f"  Transients: {self.transient_density_per_s:.1f}/s")
        lines.append("  Balance:")
        for band, db in self.spectral_balance.items():
            lines.append(f"    {band}: {db:.1f} dB")
        if self.top_resonances:
            tops = ", ".join(
                f"{r['hz']:.0f}Hz" for r in self.top_resonances
            )
            lines.append(f"  Top resonances: {tops}")
        if self.correlation is not None:
            corr_min_str = (
                f", min frame {self.correlation_min:+.2f}"
                if self.correlation_min is not None else ""
            )
            lines.append(
                f"  Stereo: correlation {self.correlation:+.2f}, "
                f"side/mid {self.side_ratio_db:.1f} dB{corr_min_str}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        # Silent or near-silent audio produces -inf in dB values (log10(0)).
        # JSON can't encode inf/nan, so floor everything at -120 dB.
        def _safe(v, floor: float = -120.0):
            if v is None or not math.isfinite(v):
                return floor
            return round(v, 1)

        def _safe_corr(v):
            if v is None or not math.isfinite(v):
                return None
            return round(max(-1.0, min(1.0, v)), 3)

        def _safe_opt(v, places: int = 1):
            if v is None or not math.isfinite(v):
                return None
            return round(v, places)

        return {
            "path": self.path,
            "sample_rate": self.sample_rate,
            "duration_s": round(self.duration_s, 2),
            "channels": self.channels,
            "lufs": _safe(self.lufs),
            "true_peak_db": _safe(self.true_peak_db),
            "spectral_centroid_hz": _safe(self.spectral_centroid_hz, floor=0.0),
            "spectral_balance": {k: _safe(v) for k, v in self.spectral_balance.items()},
            "rms_db": _safe(self.rms_db),
            "status": self.status,
            "correlation": _safe_corr(self.correlation),
            "side_ratio_db": _safe(self.side_ratio_db) if self.side_ratio_db is not None else None,
            "side_balance": (
                {k: _safe(v) for k, v in self.side_balance.items()}
                if self.side_balance is not None else None
            ),
            "crest_factor_db": _safe_opt(self.crest_factor_db),
            "lra_lu": _safe_opt(self.lra_lu),
            "transient_density_per_s": _safe_opt(self.transient_density_per_s, places=2),
            "top_resonances": self.top_resonances if self.top_resonances else None,
            "correlation_min": _safe_corr(self.correlation_min),
            "fundamental_hz": _safe_opt(self.fundamental_hz),
            "voicing_ratio": _safe_opt(self.voicing_ratio, places=2),
        }


# ── STFT ─────────────────────────────────────────────────────────────


def _stft_n_fft(sr: int) -> int:
    """Power-of-2 window size targeting ~46 ms.
    Gives 1024 @ 22.05k, 2048 @ 44.1k, 2048 @ 48k.
    """
    target_s = 0.046
    return int(2 ** round(math.log2(max(1.0, sr * target_s))))


def _compute_stft(
    mono: np.ndarray, sr: int
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Return (mag_frames, freqs, params).

    mag_frames shape: (n_frames, n_bins). Hann window, 50% overlap.
    Pads if the signal is shorter than one window.
    """
    from numpy.lib.stride_tricks import sliding_window_view

    n_fft = _stft_n_fft(sr)
    hop = n_fft // 2
    mono = np.asarray(mono, dtype=np.float32)

    if len(mono) < n_fft:
        mono = np.pad(mono, (0, n_fft - len(mono)))

    n_frames = ((len(mono) - n_fft) // hop) + 1
    needed = (n_frames - 1) * hop + n_fft
    if len(mono) > needed:
        mono = mono[:needed]

    window = np.hanning(n_fft).astype(np.float32)
    framed = sliding_window_view(mono, n_fft)[::hop]  # (n_frames, n_fft)
    spectra = np.fft.rfft(framed * window, axis=-1)
    mag = np.abs(spectra).astype(np.float32)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr).astype(np.float32)
    return mag, freqs, {"n_fft": int(n_fft), "hop": int(hop), "window": "hann"}


# ── New summary-field computations ──────────────────────────────────


def _crest_factor_db(audio: np.ndarray) -> float | None:
    """Peak-to-RMS in dB. Computed on the full signal (any channel count)."""
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms < 1e-9 or peak < 1e-9:
        return None
    return float(20.0 * math.log10(peak / rms))


def _lra_lu(audio: np.ndarray, sr: int) -> float | None:
    """Loudness Range — 95th - 10th percentile of short-term LUFS values
    (3 s windows, 1 s hop), with a loose absolute gate.

    Returns None when pyloudnorm is unavailable, the file is too short,
    or fewer than 2 measurable windows survive the gate.
    """
    try:
        import pyloudnorm as pyln
    except ImportError:
        return None
    if audio.ndim == 1:
        audio = audio[:, None]
    if len(audio) < 3 * sr:
        return None
    win = 3 * sr
    hop = sr
    n_windows = (len(audio) - win) // hop + 1
    if n_windows < 2:
        return None
    meter = pyln.Meter(sr)
    loudnesses: list[float] = []
    for i in range(n_windows):
        seg = audio[i * hop : i * hop + win]
        try:
            l = meter.integrated_loudness(seg)
        except Exception:
            continue
        if math.isfinite(l):
            loudnesses.append(float(l))
    if len(loudnesses) < 2:
        return None
    arr = np.asarray(loudnesses)
    # Loose absolute gate at -70 LUFS (mirrors BS.1770 absolute gate)
    arr = arr[arr > -70.0]
    if len(arr) < 2:
        return None
    return float(np.percentile(arr, 95) - np.percentile(arr, 10))


def _transient_density_per_s(
    stft_mag: np.ndarray, sr: int, n_fft: int, hop: int
) -> float | None:
    """Transients per second: rising edges where per-frame energy spikes
    >6 dB above a 30-frame rolling median.
    """
    if stft_mag.shape[0] < 32:
        return None
    energy = stft_mag.sum(axis=1)
    energy_db = 10.0 * np.log10(energy + 1e-10)

    from numpy.lib.stride_tricks import sliding_window_view

    win = 30
    rolled = sliding_window_view(energy_db, win)
    medians = np.median(rolled, axis=1)
    # Pad medians to align with energy_db (use the first available median for
    # the leading frames so we don't false-trigger at the start).
    pad = np.full(win - 1, medians[0])
    medians = np.concatenate([pad, medians])

    spikes = energy_db > medians + 6.0
    edges = np.diff(spikes.astype(np.int8)) == 1
    n_events = int(edges.sum())
    duration_s = stft_mag.shape[0] * hop / sr
    if duration_s <= 0:
        return None
    return float(n_events / duration_s)


def _top_resonances(
    stft_mag: np.ndarray, freqs: np.ndarray, n: int = 3
) -> list[dict]:
    """Top n spectral peaks on the frame-averaged spectrum with prominence.

    Each entry: {hz, db, q_est, prominence_db}.
    Skips DC and sub-30 Hz bins (often capture system rumble / DC offset).
    """
    if stft_mag.size == 0:
        return []
    mean_mag = stft_mag.mean(axis=0)
    if mean_mag.max() < 1e-12:
        return []
    db = 20.0 * np.log10(mean_mag + 1e-10)
    audible = freqs >= 30.0  # peak position must be in the audible range

    peaks: list[dict] = []
    # Local-max scan: prominence measured against the unmasked dB curve so
    # the audibility mask doesn't fabricate giant prominences via a sentinel.
    pw = 30
    n_bins = len(db)
    for i in range(1, n_bins - 1):
        if not audible[i]:
            continue
        if db[i] <= db[i - 1] or db[i] < db[i + 1]:
            continue
        lo = max(0, i - pw)
        hi = min(n_bins, i + pw)
        local_min = float(db[lo:hi].min())
        prominence = float(db[i] - local_min)
        if prominence < 6.0:
            continue
        # Q estimate via -3 dB bandwidth around the peak.
        target = db[i] - 3.0
        l = i
        while l > 0 and db[l] > target:
            l -= 1
        r = i
        while r < n_bins - 1 and db[r] > target:
            r += 1
        bandwidth = max(float(freqs[r] - freqs[l]), 1.0)
        q_est = float(freqs[i]) / bandwidth
        peaks.append({
            "hz": float(freqs[i]),
            "db": round(float(db[i]), 1),
            "q_est": round(q_est, 1),
            "prominence_db": round(prominence, 1),
        })

    peaks.sort(key=lambda p: p["prominence_db"], reverse=True)
    return peaks[:n]


def _correlation_min(
    audio: np.ndarray, n_fft: int, hop: int
) -> float | None:
    """Per-frame Pearson correlation between L and R, returning the min.

    Returns None on mono or when fewer than 2 measurable frames exist.
    """
    if audio.ndim < 2 or audio.shape[1] < 2:
        return None
    L = audio[:, 0].astype(np.float64)
    R = audio[:, 1].astype(np.float64)
    if len(L) < n_fft:
        return None
    n_frames = (len(L) - n_fft) // hop + 1
    if n_frames < 2:
        return None
    correlations: list[float] = []
    for i in range(n_frames):
        s = i * hop
        e = s + n_fft
        l = L[s:e]
        r = R[s:e]
        if l.std() > 1e-9 and r.std() > 1e-9:
            c = float(np.corrcoef(l, r)[0, 1])
            if math.isfinite(c):
                correlations.append(c)
    if not correlations:
        return None
    return float(min(correlations))


# ── analyze_audio ───────────────────────────────────────────────────


def _compute_lufs(audio: np.ndarray, sr: int) -> float:
    """Integrated LUFS via pyloudnorm. Falls back to RMS approximation if
    the library isn't installed or the file is shorter than pyloudnorm's
    minimum block (~400 ms)."""
    try:
        import pyloudnorm as pyln

        meter = pyln.Meter(sr)
        try:
            loudness = meter.integrated_loudness(audio)
            return float(loudness)
        except ValueError:
            # File shorter than the gating block — fall through to RMS.
            pass
    except ImportError:
        pass
    rms = np.sqrt(np.mean(audio**2))
    return float(20 * np.log10(rms + 1e-10))


def analyze_audio(path: str | Path) -> AudioAnalysis:
    """Analyze an audio file and return spectral + loudness + dynamics metrics.

    For callers that also need the STFT magnitude matrix (e.g. the cache
    layer or drill-down tools), use `analyze_audio_full()` instead.
    """
    analysis, _stft_mag, _stft_params = analyze_audio_full(path)
    return analysis


def analyze_audio_full(
    path: str | Path,
) -> tuple[AudioAnalysis, np.ndarray, dict]:
    """Full analysis with the STFT magnitude matrix exposed.

    Returns `(analysis, stft_mag, stft_params)`. The matrix is float32; the
    cache layer will downcast to float16 on persist.
    """
    path = Path(path)
    audio, sr = sf.read(str(path), dtype="float64")
    return analyze_array(audio, int(sr), source_path=str(path))


def analyze_array(
    audio: np.ndarray,
    sr: int,
    *,
    source_path: str = "<array>",
) -> tuple[AudioAnalysis, np.ndarray, dict]:
    """Run the full analyzer over an in-memory audio array.

    `audio` may be 1-D mono or 2-D `(N, channels)`. Used by
    `analyze_audio_full` (after a soundfile read) and by the drill-down
    tools (which slice a section of an already-loaded WAV before
    analysing).
    """
    if audio.ndim == 1:
        audio = audio.reshape(-1, 1)
    num_channels = audio.shape[1]
    duration = len(audio) / sr
    mono = audio.mean(axis=1)

    # Whole-file scalars (peak, RMS, LUFS)
    true_peak = float(np.max(np.abs(audio)))
    true_peak_db = 20.0 * math.log10(true_peak + 1e-10)
    rms = float(np.sqrt(np.mean(mono**2)))
    rms_db = 20.0 * math.log10(rms + 1e-10)
    lufs = _compute_lufs(audio, sr)

    # STFT — single source for time-aware metrics
    stft_mag, stft_freqs, stft_params = _compute_stft(mono, sr)

    # Whole-file FFT for spectral_balance / centroid (preserved for
    # backward compatibility with the existing summary contract).
    fft_result = np.fft.rfft(mono)
    fft_magnitude = np.abs(fft_result)
    fft_freqs = np.fft.rfftfreq(len(mono), 1.0 / sr)

    total_energy = float(np.sum(fft_magnitude))
    spectral_centroid = (
        float(np.sum(fft_freqs * fft_magnitude) / total_energy)
        if total_energy > 0 else 0.0
    )

    spectral_balance: dict[str, float] = {
        band_name: float(band_energy_db(fft_magnitude, fft_freqs, f_low, f_high))
        for band_name, (f_low, f_high) in BANDS.items()
    }

    status = "silent" if (not math.isfinite(rms_db) or rms_db < -60.0) else "ok"

    # Stereo whole-file
    correlation: float | None = None
    side_ratio_db: float | None = None
    side_balance: dict[str, float] | None = None
    correlation_min_val: float | None = None

    if num_channels >= 2:
        L = audio[:, 0]
        R = audio[:, 1]
        if L.std() > 1e-9 and R.std() > 1e-9:
            correlation = float(np.corrcoef(L, R)[0, 1])
        else:
            correlation = 1.0  # silent → trivially "mono"

        mid = 0.5 * (L + R)
        side = 0.5 * (L - R)
        mid_rms = float(np.sqrt(np.mean(mid**2)))
        side_rms = float(np.sqrt(np.mean(side**2)))
        if mid_rms > 1e-9:
            side_ratio_db = 20.0 * math.log10((side_rms + 1e-12) / mid_rms)
        else:
            side_ratio_db = float("inf")

        side_fft = np.abs(np.fft.rfft(side))
        side_balance = {
            band_name: float(band_energy_db(side_fft, fft_freqs, f_low, f_high))
            for band_name, (f_low, f_high) in BANDS.items()
        }

        correlation_min_val = _correlation_min(audio, stft_params["n_fft"], stft_params["hop"])

    crest_factor_db = _crest_factor_db(audio)
    lra_lu = _lra_lu(audio, sr)
    transient_density = _transient_density_per_s(
        stft_mag, sr, stft_params["n_fft"], stft_params["hop"]
    )
    top_resonances = _top_resonances(stft_mag, stft_freqs, n=3)

    # Tonal layer — runs its own framing (longer window than STFT for
    # low-pitch reach). Pure numpy, fast enough to run on every analyse.
    from studiomind.analyzer.tonal import estimate_fundamental
    fundamental_hz, voicing_ratio = estimate_fundamental(mono, int(sr))

    analysis = AudioAnalysis(
        path=str(source_path),
        sample_rate=int(sr),
        duration_s=float(duration),
        channels=int(num_channels),
        lufs=float(lufs),
        true_peak_db=float(true_peak_db),
        spectral_centroid_hz=float(spectral_centroid),
        spectral_balance=spectral_balance,
        rms_db=float(rms_db),
        status=status,
        correlation=correlation,
        side_ratio_db=side_ratio_db,
        side_balance=side_balance,
        crest_factor_db=crest_factor_db,
        lra_lu=lra_lu,
        transient_density_per_s=transient_density,
        top_resonances=top_resonances,
        correlation_min=correlation_min_val,
        fundamental_hz=fundamental_hz,
        voicing_ratio=voicing_ratio,
    )
    return analysis, stft_mag, stft_params


def detect_masking(analyses: list[AudioAnalysis]) -> list[dict]:
    """Detect frequency bands where multiple stems compete (whole-file).

    Time-aware masking lives in analyzer/masking.py once it's wired up;
    this whole-file flavor is preserved as a quick sanity check.
    """
    if len(analyses) < 2:
        return []

    conflicts: list[dict] = []
    for band_name in BANDS:
        loud_stems = []
        for a in analyses:
            db = a.spectral_balance.get(band_name, -120)
            if db > -40:
                loud_stems.append({"path": Path(a.path).name, "db": db})

        if len(loud_stems) >= 2:
            loud_stems.sort(key=lambda x: x["db"], reverse=True)
            severity = "high" if len(loud_stems) >= 3 else "medium"
            conflicts.append({
                "band": band_name,
                "frequency_range": BANDS[band_name],
                "stems": loud_stems,
                "severity": severity,
            })

    return conflicts
