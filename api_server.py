"""
Audio Separator — GPU-accelerated audio stem separation FastAPI service.

Architecture:
  - asyncio.Lock: serialized GPU access, concurrent requests queue automatically
  - MD5 cache: same file + same params within 1h returns cached result instantly
  - Client sets timeout=300s, no polling needed
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import platform
import time
import uuid
from pathlib import Path
from typing import Optional

import uvicorn
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from audio_separator.separator import Separator

# ── Config ─────────────────────────────────────────────────────────────────
SERVICE_PORT = int(os.getenv("AS_PORT", "80"))
MODEL_DIR = os.getenv("AS_MODEL_DIR", os.path.join(os.path.dirname(__file__), "models"))
OUTPUT_DIR = os.getenv("AS_OUTPUT_DIR", os.path.join(os.path.dirname(__file__), "output"))
LOG_DIR = os.getenv("AS_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))
CACHE_DIR = os.getenv("AS_CACHE_DIR", os.path.join(os.path.dirname(__file__), "cache"))
DEFAULT_ENSEMBLE_PRESET = os.getenv("AS_DEFAULT_PRESET", "vocal_balanced")
CACHE_TTL_SEC = int(os.getenv("AS_CACHE_TTL", "3600"))
GPU_MEMORY_FRACTION = float(os.getenv("AS_GPU_FRACTION", "0.9"))

for d in (MODEL_DIR, OUTPUT_DIR, LOG_DIR, CACHE_DIR):
    os.makedirs(d, exist_ok=True)

# ── Resource Limits ────────────────────────────────────────────────────────


def _apply_resource_limits():
    if torch.cuda.is_available():
        total_gpu = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(GPU_MEMORY_FRACTION)
        logger.info(
            f"GPU mem limit: {GPU_MEMORY_FRACTION*100:.0f}% "
            f"({total_gpu * GPU_MEMORY_FRACTION / 1024**3:.1f} GB / "
            f"{total_gpu / 1024**3:.1f} GB)"
        )

    try:
        import psutil
        pid = os.getpid()
        proc = psutil.Process(pid)
        if platform.system() == "Windows":
            proc.nice(psutil.HIGH_PRIORITY_CLASS)
        cpu_count = psutil.cpu_count(logical=True)
        if cpu_count and cpu_count > 1:
            half = cpu_count // 2
            proc.cpu_affinity(list(range(half)))
            logger.info(f"CPU affinity set to {half}/{cpu_count} cores")
    except ImportError:
        pass
    except Exception:
        pass


# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "api_server.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("audio_api")

app = FastAPI(title="Audio Separator", version="3.0.0", docs_url="/docs")

# ── GPU Lock ───────────────────────────────────────────────────────────────
_gpu_lock = asyncio.Lock()
_queue_count: int = 0


@app.middleware("http")
async def _track_queue_middleware(request, call_next):
    global _queue_count
    if request.url.path in ("/separate", "/separate/download"):
        _queue_count += 1
        try:
            return await call_next(request)
        finally:
            _queue_count -= 1
    return await call_next(request)


# ── MD5 Result Cache ──────────────────────────────────────────────────────
_cache: dict[str, tuple] = {}
_cache_lock = asyncio.Lock()


def _cache_key(content_hash: str, preset: str, fmt: str, single_stem: str) -> str:
    return f"md5:{content_hash}:preset:{preset}:fmt:{fmt}:stem:{single_stem or ''}"


async def _cache_get(key: str):
    async with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > time.time():
            return entry[1]
        _cache.pop(key, None)
        return None


async def _cache_set(key: str, data, ttl=CACHE_TTL_SEC):
    async with _cache_lock:
        _cache[key] = (time.time() + ttl, data)


async def _cache_cleanup():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        async with _cache_lock:
            expired = [k for k, (exp, _) in _cache.items() if exp <= now]
            for k in expired:
                del _cache[k]
        if expired:
            logger.info(f"Cache cleanup: removed {len(expired)} entries ({len(_cache)} remain)")


# ── Model Cache ────────────────────────────────────────────────────────────
_sep_cache: dict[str, Separator] = {}

ENSEMBLE_PRESETS = {
    "vocal_balanced": "Best overall vocals — Resurrection + Beta 6X (avg_fft)",
    "vocal_clean": "Minimal instrument bleed — Revive V2 + FT2 bleedless (min_fft)",
    "vocal_full": "Max vocal capture incl. harmonies — Revive 3e + becruily (max_fft)",
    "vocal_rvc": "Optimized for RVC training — Beta 6X + Gabox FV4 (avg_wave)",
    "instrumental_clean": "Cleanest instrumentals, minimal vocal bleed (uvr_max_spec)",
    "instrumental_full": "Max instrument preservation (uvr_max_spec)",
    "instrumental_balanced": "Good balance — INSTV8 + Resurrection Inst (uvr_max_spec)",
    "instrumental_low_resource": "Fast ensemble for low VRAM (avg_fft)",
    "karaoke": "Lead vocal removal — 3-model karaoke (avg_wave)",
}


def _get_separator(model_filename: str = None, ensemble_preset: str = None) -> Separator:
    global _sep_cache
    key = f"preset:{ensemble_preset}" if ensemble_preset else (f"model:{model_filename}" if model_filename else f"preset:{DEFAULT_ENSEMBLE_PRESET}")
    if key in _sep_cache:
        return _sep_cache[key]

    logger.info(f"Loading separator: {key}")
    mdxc = {"segment_size": 256, "override_model_segment_size": True, "batch_size": 1, "overlap": 8, "pitch_shift": 0}

    kwargs = dict(log_level=logging.INFO, model_file_dir=MODEL_DIR, output_dir=OUTPUT_DIR,
                  output_format="WAV", normalization_threshold=0.9, use_autocast=True)

    if ensemble_preset:
        sep = Separator(ensemble_preset=ensemble_preset, mdxc_params=mdxc, **kwargs)
        sep.load_model()
    else:
        fn = model_filename or DEFAULT_ENSEMBLE_PRESET
        if fn in ENSEMBLE_PRESETS:
            sep = Separator(ensemble_preset=fn, mdxc_params=mdxc, **kwargs)
            sep.load_model()
        else:
            sep = Separator(**kwargs)
            sep.load_model(model_filename=fn)

    _sep_cache[key] = sep
    if len(_sep_cache) > 3:
        oldest = next(iter(_sep_cache))
        try:
            del _sep_cache[oldest]
        except Exception:
            pass
    return sep


# ── Helpers ────────────────────────────────────────────────────────────────


def _load_model_index() -> dict:
    import importlib.resources as res
    try:
        return json.loads(res.read_text("audio_separator", "models.json"))
    except Exception:
        return json.loads(Path("/g/audio/audio_separator/models.json").read_text(encoding="utf-8"))


def _list_models() -> list[dict]:
    idx = _load_model_index()
    results = []
    for cat, entries in idx.items():
        if isinstance(entries, dict):
            for name, fname in entries.items():
                dl = fname if isinstance(fname, str) else (list(fname.keys())[0] if isinstance(fname, dict) else str(fname))
                results.append({"category": cat, "display_name": name, "download_filename": dl})
        elif isinstance(entries, str):
            results.append({"category": cat, "display_name": cat, "download_filename": entries})
    return results


# ── Core separation ────────────────────────────────────────────────────────


def _run_separation(input_path, ensemble_preset, model_filename, output_format, single_stem) -> dict:
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    sep = _get_separator(model_filename=model_filename, ensemble_preset=ensemble_preset)
    sep.output_format = output_format
    sep.output_single_stem = single_stem

    t0 = time.perf_counter()
    output_files = sep.separate(input_path)
    elapsed = time.perf_counter() - t0

    output_files = [os.path.join(OUTPUT_DIR, f) if not os.path.isabs(f) else f for f in output_files]
    stem_names = [Path(fp).stem for fp in output_files]

    return {"stem_names": stem_names, "output_files": output_files, "duration_seconds": round(elapsed, 2)}


async def _process_or_cache(content: bytes, filename: str, input_size_mb: float,
                            ensemble_preset: Optional[str], model_filename: Optional[str],
                            output_format: str, single_stem: Optional[str],
                            return_zip: bool = False):
    content_hash = hashlib.md5(content).hexdigest()
    preset = ensemble_preset or DEFAULT_ENSEMBLE_PRESET
    ck = _cache_key(content_hash, preset, output_format, single_stem or '')

    cached = await _cache_get(ck)
    if cached is not None:
        logger.info(f"[cache HIT] {filename} (md5={content_hash[:8]}...)")
        return cached, True

    logger.info(f"[cache MISS] {filename} md5={content_hash[:8]}...")

    ext = Path(filename).suffix or ".wav"
    input_path = os.path.join(OUTPUT_DIR, f"input_{uuid.uuid4().hex[:12]}{ext}")
    with open(input_path, "wb") as f:
        f.write(content)

    try:
        async with _gpu_lock:
            logger.info(f"[gpu] started: {filename}")
            result = await asyncio.to_thread(
                _run_separation, input_path, ensemble_preset, model_filename, output_format, single_stem
            )
    finally:
        try:
            os.remove(input_path)
        except Exception:
            pass

    if return_zip:
        import zipfile
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in result["output_files"]:
                if os.path.exists(fp):
                    zf.write(fp, f"{Path(fp).stem}.{output_format.lower()}")
        zip_buf.seek(0)
        await _cache_set(ck, zip_buf.getvalue())
        for fp in result["output_files"]:
            try:
                os.remove(fp)
            except Exception:
                pass
        return zip_buf.getvalue(), False
    else:
        resp = {
            "status": "ok",
            "duration_seconds": result["duration_seconds"],
            "input_file": filename,
            "input_size_mb": round(input_size_mb, 2),
            "preset": preset,
            "output_format": output_format,
            "stems": result["stem_names"],
            "cached": False,
        }
        await _cache_set(ck, json.dumps(resp).encode("utf-8"))
        for fp in result["output_files"]:
            try:
                os.remove(fp)
            except Exception:
                pass
        return resp, False


# ── Startup ────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup():
    _apply_resource_limits()
    asyncio.create_task(_cache_cleanup())

    logger.info(f"Pre-warming: {DEFAULT_ENSEMBLE_PRESET}")
    try:
        _get_separator(ensemble_preset=DEFAULT_ENSEMBLE_PRESET)
        logger.info("Pre-warm complete.")
    except Exception as e:
        logger.warning(f"Pre-warm failed: {e}")


# ── Routes ─────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    info = {
        "status": "ok",
        "cuda_available": torch.cuda.is_available(),
        "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "gpu_memory_limit": (
            f"{torch.cuda.get_device_properties(0).total_memory * GPU_MEMORY_FRACTION / 1024**3:.1f} GB "
            f"({GPU_MEMORY_FRACTION*100:.0f}%)"
            if torch.cuda.is_available() else "N/A"
        ),
        "default_preset": DEFAULT_ENSEMBLE_PRESET,
        "queue": {"waiting_or_active": max(0, _queue_count), "gpu_busy": _gpu_lock.locked()},
        "cache": {"entries": len(_cache), "ttl_sec": CACHE_TTL_SEC},
    }
    try:
        import psutil
        info["cpu_affinity"] = str(psutil.Process(os.getpid()).cpu_affinity())
    except Exception:
        pass
    return info


@app.get("/queue")
async def queue_status():
    return {
        "waiting_or_active": max(0, _queue_count),
        "gpu_busy": _gpu_lock.locked(),
        "cache_entries": len(_cache),
    }


@app.get("/models")
async def list_models():
    return {"count": 0, "models": _list_models()}


@app.get("/presets")
async def list_presets():
    return {"count": len(ENSEMBLE_PRESETS), "default": DEFAULT_ENSEMBLE_PRESET, "presets": ENSEMBLE_PRESETS}


@app.post("/separate")
async def separate_audio(
    file: UploadFile = File(...),
    ensemble_preset: Optional[str] = Form(default=None),
    model_filename: Optional[str] = Form(default=None),
    output_format: str = Form(default="WAV"),
    single_stem: Optional[str] = Form(default=None),
):
    output_format = output_format.upper()
    if output_format not in ("WAV", "FLAC", "MP3", "OGG", "M4A"):
        raise HTTPException(400, f"Unsupported format: {output_format}")

    content = await file.read()
    input_size_mb = len(content) / (1024 * 1024)

    result, from_cache = await _process_or_cache(
        content, file.filename, input_size_mb,
        ensemble_preset, model_filename, output_format, single_stem,
        return_zip=False,
    )

    if from_cache and isinstance(result, bytes):
        result = json.loads(result.decode("utf-8"))
    result["cached"] = from_cache
    return result


@app.post("/separate/download")
async def separate_and_download(
    file: UploadFile = File(...),
    ensemble_preset: Optional[str] = Form(default=None),
    model_filename: Optional[str] = Form(default=None),
    output_format: str = Form(default="WAV"),
    single_stem: Optional[str] = Form(default=None),
):
    output_format = output_format.upper()
    if output_format not in ("WAV", "FLAC", "MP3", "OGG", "M4A"):
        raise HTTPException(400, f"Unsupported format: {output_format}")

    content = await file.read()
    input_size_mb = len(content) / (1024 * 1024)

    zip_bytes, from_cache = await _process_or_cache(
        content, file.filename, input_size_mb,
        ensemble_preset, model_filename, output_format, single_stem,
        return_zip=True,
    )

    base_name = Path(file.filename).stem
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{base_name}_separated.zip"',
            "X-Cached": str(from_cache).lower(),
        },
    )


# ── Entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"Starting Audio Separator on port {SERVICE_PORT}...")
    uvicorn.run("api_server:app", host="0.0.0.0", port=SERVICE_PORT, log_level="info", workers=1)
