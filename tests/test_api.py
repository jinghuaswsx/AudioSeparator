"""Integration tests for a running Audio Separator API service."""

import os

import pytest
import requests

API = os.getenv("AS_API", "").rstrip("/")
TIMEOUT = 120

pytestmark = pytest.mark.skipif(
    not API,
    reason="set AS_API to a running Audio Separator service to run integration tests",
)


def _create_wav(path: str, duration: int = 10):
    import numpy as np
    import soundfile as sf
    sr = 44100
    t = np.linspace(0, duration, sr * duration, endpoint=False)
    sf.write(path, (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sr)


def test_health():
    r = requests.get(f"{API}/health", timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_presets():
    r = requests.get(f"{API}/presets", timeout=10)
    assert r.status_code == 200
    assert r.json()["count"] >= 9


def test_separate(tmp_path):
    wav = os.path.join(tmp_path, "test.wav")
    _create_wav(wav)
    with open(wav, "rb") as f:
        r = requests.post(f"{API}/separate", files={"file": f}, timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    assert len(data["stems"]) >= 2
    assert any("Vocals" in s for s in data["stems"])


def test_download(tmp_path):
    wav = os.path.join(tmp_path, "test.wav")
    _create_wav(wav)
    with open(wav, "rb") as f:
        r = requests.post(f"{API}/separate/download", files={"file": f}, timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert len(r.content) > 100
