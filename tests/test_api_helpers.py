import pytest
from fastapi import HTTPException

import api_server
from api_server import (
    DEFAULT_ENSEMBLE_PRESET,
    _cache_key,
    _process_or_cache,
    _resolve_effective_preset,
    list_presets,
)


def test_resolve_goal_background_preserve_to_instrumental_full():
    assert _resolve_effective_preset(None, None, "background_preserve") == "instrumental_full"


def test_resolve_explicit_preset_overrides_goal():
    assert _resolve_effective_preset("vocal_balanced", None, "background_preserve") == "vocal_balanced"


def test_resolve_single_model_is_not_overridden_by_goal():
    assert _resolve_effective_preset(None, "some_model.ckpt", "background_preserve") == DEFAULT_ENSEMBLE_PRESET


def test_resolve_unknown_goal_rejects_request():
    with pytest.raises(HTTPException) as excinfo:
        _resolve_effective_preset(None, None, "unknown")
    assert excinfo.value.status_code == 400


def test_list_presets_exposes_separation_goals():
    import asyncio

    payload = asyncio.run(list_presets())

    assert payload["default_goal"] == "vocal_quality"
    assert payload["goals"]["background_preserve"]["preset"] == "instrumental_full"


def test_cache_key_separates_json_zip_and_single_model():
    json_key = _cache_key("abc", "vocal_balanced", "WAV", "", "json", "")
    zip_key = _cache_key("abc", "vocal_balanced", "WAV", "", "zip", "")
    model_key = _cache_key("abc", "vocal_balanced", "WAV", "", "json", "x.ckpt")

    assert json_key != zip_key
    assert json_key != model_key


def test_process_or_cache_uses_goal_preset_and_isolates_response_modes(monkeypatch, tmp_path):
    calls = []

    def fake_run(input_path, ensemble_preset, model_filename, output_format, single_stem):
        calls.append((ensemble_preset, model_filename, output_format, single_stem))
        out = tmp_path / f"out_{len(calls)}.wav"
        out.write_bytes(b"WAVDATA")
        return {
            "stem_names": [out.stem],
            "output_files": [str(out)],
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr(api_server, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(api_server, "_run_separation", fake_run)
    api_server._cache.clear()

    async def run():
        json_result, json_cached = await _process_or_cache(
            b"audio", "input.wav", 0.1,
            None, None, "background_preserve", "WAV", None,
            return_zip=False,
        )
        zip_result, zip_cached = await _process_or_cache(
            b"audio", "input.wav", 0.1,
            None, None, "background_preserve", "WAV", None,
            return_zip=True,
        )
        return json_result, json_cached, zip_result, zip_cached

    import asyncio
    json_result, json_cached, zip_result, zip_cached = asyncio.run(run())

    assert calls == [
        ("instrumental_full", None, "WAV", None),
        ("instrumental_full", None, "WAV", None),
    ]
    assert json_result["preset"] == "instrumental_full"
    assert json_cached is False
    assert zip_result.startswith(b"PK")
    assert zip_cached is False
