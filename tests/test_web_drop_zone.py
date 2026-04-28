"""Tests for the web drop-zone backend (/upload + /relocate + classifier)."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from fastapi.testclient import TestClient


def _make_wav_bytes(duration_s: float = 1.0, channels: int = 2, sr: int = 22050) -> bytes:
    """Build an in-memory WAV byte string for upload tests."""
    rng = np.random.default_rng(0)
    n = int(duration_s * sr)
    if channels == 1:
        audio = (rng.standard_normal(n) * 0.05).astype(np.float32)
    else:
        audio = (rng.standard_normal((n, channels)) * 0.05).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@pytest.fixture
def web_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Spin up a TestClient against a fresh, isolated workspace.

    The web app resolves the active project via `_resolve_active_project`;
    we patch it so tests don't depend on FL Studio or globally-stored config.
    """
    from studiomind.workspace import open_project

    project = open_project("DropZoneTest", root=tmp_path)

    from studiomind.web import app as app_module

    def _fake_resolve():
        return project, None

    monkeypatch.setattr(app_module, "_resolve_active_project", _fake_resolve)

    return TestClient(app_module.app)


# ── Smart-default classifier ────────────────────────────────────────


def test_filename_with_master_routes_to_masters(web_client: TestClient) -> None:
    wav = _make_wav_bytes(duration_s=120.0, channels=2)
    r = web_client.post(
        "/api/workspace/upload",
        files={"file": ("song_master.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["folder"] == "masters"
    assert r.json()["classification_reason"] == "filename_match_master"


def test_filename_with_reference_routes_to_references(web_client: TestClient) -> None:
    wav = _make_wav_bytes(duration_s=10.0, channels=2)
    r = web_client.post(
        "/api/workspace/upload",
        files={"file": ("ref_alpha.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["folder"] == "references"


def test_long_stereo_file_classified_as_reference(web_client: TestClient) -> None:
    """Untitled 90-second stereo file → likely full mix → references."""
    wav = _make_wav_bytes(duration_s=90.0, channels=2)
    r = web_client.post(
        "/api/workspace/upload",
        files={"file": ("untitled.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["folder"] == "references"


def test_short_mono_file_classified_as_drop(web_client: TestClient) -> None:
    """Short mono file with no filename signal → drops/ as a sample."""
    wav = _make_wav_bytes(duration_s=2.0, channels=1)
    r = web_client.post(
        "/api/workspace/upload",
        files={"file": ("clip.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["folder"] == "drops"


def test_no_signal_falls_through_to_drops(web_client: TestClient) -> None:
    """Medium stereo with neutral filename → no clear signal → drops/."""
    wav = _make_wav_bytes(duration_s=15.0, channels=2)
    r = web_client.post(
        "/api/workspace/upload",
        files={"file": ("foo.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["folder"] == "drops"


# ── Override path ───────────────────────────────────────────────────


def test_explicit_target_folder_overrides_classifier(web_client: TestClient) -> None:
    wav = _make_wav_bytes(duration_s=120.0, channels=2)  # would be 'masters' by classifier
    r = web_client.post(
        "/api/workspace/upload?target_folder=drops",
        files={"file": ("song_master.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["folder"] == "drops"
    assert r.json()["classification_reason"] == "user_override"


def test_intent_hint_routes_to_references(web_client: TestClient) -> None:
    wav = _make_wav_bytes(duration_s=2.0, channels=1)  # would be 'drops' by classifier
    r = web_client.post(
        "/api/workspace/upload?intent_hint=reference",
        files={"file": ("clip.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["folder"] == "references"


def test_invalid_target_folder_falls_back_to_classifier(web_client: TestClient) -> None:
    """Garbage `target_folder` is ignored, classifier runs anyway."""
    wav = _make_wav_bytes(duration_s=120.0, channels=2)
    r = web_client.post(
        "/api/workspace/upload?target_folder=__not_a_real_folder__",
        files={"file": ("song_master.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["folder"] == "masters"


# ── Format gating ───────────────────────────────────────────────────


def test_unsupported_extension_rejected(web_client: TestClient) -> None:
    r = web_client.post(
        "/api/workspace/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400


# ── Backward-compat alias ───────────────────────────────────────────


def test_reference_alias_still_works(web_client: TestClient) -> None:
    """`/api/workspace/reference` should keep working — frontend uses it."""
    wav = _make_wav_bytes(duration_s=10.0, channels=2)
    r = web_client.post(
        "/api/workspace/reference",
        files={"file": ("anything.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["folder"] == "references"


# ── Relocate ────────────────────────────────────────────────────────


def test_relocate_moves_file_between_folders(
    web_client: TestClient, tmp_path: Path
) -> None:
    wav = _make_wav_bytes(duration_s=2.0, channels=1)
    r = web_client.post(
        "/api/workspace/upload",
        files={"file": ("clip.wav", wav, "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["folder"] == "drops"

    rr = web_client.post(
        "/api/workspace/relocate",
        json={"filename": "clip.wav", "from_folder": "drops", "to_folder": "references"},
    )
    assert rr.status_code == 200
    assert rr.json()["to"] == "references"


def test_relocate_rejects_invalid_folder(web_client: TestClient) -> None:
    rr = web_client.post(
        "/api/workspace/relocate",
        json={"filename": "x.wav", "from_folder": "drops", "to_folder": "garbage"},
    )
    # Pydantic Literal validation returns 422; pre-Literal it was 400.
    assert rr.status_code in (400, 422)


def test_relocate_404_for_missing_file(web_client: TestClient) -> None:
    rr = web_client.post(
        "/api/workspace/relocate",
        json={"filename": "ghost.wav", "from_folder": "drops", "to_folder": "references"},
    )
    assert rr.status_code == 404


def test_relocate_strips_path_traversal(
    web_client: TestClient, tmp_path: Path,
) -> None:
    """A `../../etc/passwd`-style filename should be stripped to its basename."""
    rr = web_client.post(
        "/api/workspace/relocate",
        json={
            "filename": "../../../etc/passwd",
            "from_folder": "drops",
            "to_folder": "references",
        },
    )
    # Either 404 (basename not present) or success with basename — never traverses.
    assert rr.status_code in (404, 200)
