# Audio Separator API 文档

GPU 加速的音频人声/伴奏分离 HTTP 接口。FastAPI 实现，单进程串行 GPU 任务，自带 MD5 缓存。

- **服务地址**：`http://<host>:<port>`（本机生产部署使用 `http://127.0.0.1:83`）
- **Swagger UI**：`http://<host>:<port>/docs`
- **OpenAPI JSON**：`http://<host>:<port>/openapi.json`
- **协议**：HTTP/1.1，请求体 `multipart/form-data`，响应 `application/json` 或 `application/zip`
- **鉴权**：无（仅供内网访问）

---

## 目录

1. [同步 vs 异步](#同步-vs-异步)
2. [输入输出格式](#输入输出格式)
3. [并发与队列模型](#并发与队列模型)
4. [缓存](#缓存)
5. [端点列表](#端点列表)
   - 同步
     - [GET /health](#get-health)
     - [GET /queue](#get-queue)
     - [GET /models](#get-models)
     - [GET /presets](#get-presets)
     - [POST /separate](#post-separate)
     - [POST /separate/download](#post-separatedownload)
   - 异步
     - [POST /separate_async](#post-separate_async)
     - [POST /separate/download_async](#post-separatedownload_async)
     - [GET /tasks/{task_id}](#get-tasks_task_id)
     - [GET /tasks/{task_id}/download](#get-tasks_task_id_download)
6. [异步完整用例](#异步完整用例)
7. [错误码](#错误码)
8. [性能基准](#性能基准)
9. [部署快查](#部署快查)

---

## 同步 vs 异步

服务同时提供两种调用方式，**底层共享同一把 GPU 锁，串行执行**，互不抢占。

| | 同步 (`POST /separate`, `POST /separate/download`) | 异步 (`POST /separate_async`, `POST /separate/download_async`) |
|---|---|---|
| 返回时机 | 阻塞直到分离完成 | 立即返回 `task_id`（< 50 ms） |
| 客户端做的事 | 设大 timeout 等结果 | 用 `task_id` 轮询 `/tasks/{id}` |
| 适合场景 | 短任务（≤ 5 min）、客户端能保持连接 | 长任务、断线重连、批量提交、Web UI 进度展示 |
| 结果保留时长 | 同步返回后即丢弃文件 | `AS_ASYNC_TTL` 默认 **1 h**（任务完成后） |
| 队列计数 | 进入即 +1，返回即 -1 | 后台 worker 真正开始执行才 +1 |
| GPU 调度 | 与异步任务**共用同一把锁**，按提交顺序串行 | 同左 |

> 推荐：**< 60 s 文件用同步**（API 简单、低延迟）；**> 60 s 或不确定耗时用异步**（避免长连接超时）。

---

## 输入输出格式

### 输入文件
- **支持的容器/编码**：服务端没有输入格式白名单，凡 ffmpeg 能解码的均可——`mp3` / `mp4` / `m4a` / `wav` / `flac` / `ogg` / `mkv` / `mov` / `webm` 等。视频会自动抽取音轨进行分离。
- **大小**：服务端无显式上限，但请求体一次性读入内存。建议单文件 ≤ 500 MB，避免占用过多 RAM。
- **时长**：服务端无显式时长限制；客户端建议设置 ≥ 300s 的超时。

### 输出格式（`output_format` 字段）
| 取值 | 说明 |
|---|---|
| `WAV`（默认） | 无损，体积大 |
| `FLAC` | 无损，体积约为 WAV 的 50–60% |
| `MP3` | 有损，最小 |
| `OGG` | 有损 |
| `M4A` | 有损 |

非合法值返回 HTTP 400。

---

## 并发与队列模型

- 单一 `asyncio.Lock` 串行 GPU 任务；同一时刻只有 1 个分离任务在 GPU 上跑。
- 后到的请求**自动在服务端排队**，无需轮询；客户端只需把 timeout 设大（建议 300s）。
- `/health` 与 `/queue` 端点提供队列深度和 GPU 忙闲状态。

---

## 缓存

- **键**：`MD5(content) + preset + output_format + single_stem`。
- **TTL**：默认 1 小时（环境变量 `AS_CACHE_TTL`，单位秒）。
- **存储**：内存（进程内 dict），重启即清。
- **命中**：相同文件 + 相同参数二次请求 < 10 ms。
- **响应中的 `cached` 字段**为 `true` 表示来自缓存。

---

## 端点列表

### `GET /health`

健康检查 + 运行时状态。

**响应** `200 OK`
```json
{
  "status": "ok",
  "cuda_available": true,
  "cuda_device": "NVIDIA GeForce RTX 4070 Ti SUPER",
  "gpu_memory_limit": "14.0 GB (90%)",
  "default_preset": "vocal_balanced",
  "queue": {
    "waiting_or_active": 0,
    "gpu_busy": false
  },
  "cache": {
    "entries": 0,
    "ttl_sec": 3600
  },
  "cpu_affinity": "[0, 1, 2, ..., 23]"
}
```

| 字段 | 含义 |
|---|---|
| `cuda_available` | `torch.cuda.is_available()` 结果 |
| `gpu_memory_limit` | 进程 PyTorch 显存上限（onnxruntime 不受此约束） |
| `queue.waiting_or_active` | 已收下、尚未完成的请求数（含正在 GPU 上跑的那一条） |
| `queue.gpu_busy` | GPU 上是否有任务在跑 |
| `cache.entries` | 当前缓存条目数 |

---

### `GET /queue`

队列深度速查（`/health` 的子集，更轻量）。

**响应** `200 OK`
```json
{ "waiting_or_active": 0, "gpu_busy": false, "cache_entries": 0 }
```

---

### `GET /models`

列出 `models/` 目录下已下载的可指定模型。

**响应** `200 OK`
```json
{ "count": 0, "models": [] }
```

> 注：单模型分离是高级用法（通过 `model_filename` 字段使用）。日常推荐用预设（`ensemble_preset`）。

---

### `GET /presets`

列出所有集成预设。

**响应** `200 OK`
```json
{
  "count": 9,
  "default": "vocal_balanced",
  "presets": {
    "vocal_balanced":          "Best overall vocals — Resurrection + Beta 6X (avg_fft)",
    "vocal_clean":             "Minimal instrument bleed — Revive V2 + FT2 bleedless (min_fft)",
    "vocal_full":              "Max vocal capture incl. harmonies — Revive 3e + becruily (max_fft)",
    "vocal_rvc":               "Optimized for RVC training — Beta 6X + Gabox FV4 (avg_wave)",
    "instrumental_clean":      "Cleanest instrumentals, minimal vocal bleed (uvr_max_spec)",
    "instrumental_full":       "Max instrument preservation (uvr_max_spec)",
    "instrumental_balanced":   "Good balance — INSTV8 + Resurrection Inst (uvr_max_spec)",
    "instrumental_low_resource": "Fast ensemble for low VRAM (avg_fft)",
    "karaoke":                 "Lead vocal removal — 3-model karaoke (avg_wave)"
  }
}
```

---

### `POST /separate`

上传音频/视频，分离后**返回 JSON 元数据**（不返回二进制；分离结果文件被服务端缓存，需要文件本体请用 `/separate/download`）。

**Content-Type**：`multipart/form-data`

**表单字段**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `file` | 文件 | 是 | — | 音频或视频 |
| `ensemble_preset` | string | 否 | `vocal_balanced`（环境变量 `AS_DEFAULT_PRESET` 决定） | 见 `/presets` |
| `model_filename` | string | 否 | — | 直接指定单个模型文件（高级，与 `ensemble_preset` 互斥；优先级低于 `ensemble_preset`） |
| `output_format` | string | 否 | `WAV` | `WAV` / `FLAC` / `MP3` / `OGG` / `M4A` |
| `single_stem` | string | 否 | — | 仅返回某一路 stem（如 `Vocals`、`Instrumental`），其它丢弃 |

**响应** `200 OK`
```json
{
  "status": "ok",
  "duration_seconds": 9.9,
  "input_file": "song.mp4",
  "input_size_mb": 17.16,
  "preset": "vocal_balanced",
  "output_format": "WAV",
  "stems": [
    "input_c93255cbd81f_(Instrumental)_preset_vocal_balanced",
    "input_c93255cbd81f_(Vocals)_preset_vocal_balanced"
  ],
  "cached": false
}
```

| 字段 | 含义 |
|---|---|
| `duration_seconds` | 服务端纯处理耗时（不含网络 IO） |
| `input_size_mb` | 上传文件大小 |
| `stems` | 输出的 stem 名（不含扩展名）。注意：当前实现不返回文件 URL；需要文件本体时请使用 `/separate/download` |
| `cached` | 本次结果是否来自缓存 |

**示例（curl）**
```bash
curl -X POST http://127.0.0.1:83/separate \
  -F "file=@song.mp4" \
  -F "ensemble_preset=vocal_balanced" \
  -F "output_format=WAV" \
  --max-time 300
```

**示例（Python）**
```python
import requests
with open("song.mp4", "rb") as f:
    r = requests.post(
        "http://127.0.0.1:83/separate",
        files={"file": f},
        data={"ensemble_preset": "vocal_balanced", "output_format": "WAV"},
        timeout=300,
    )
print(r.json())
```

---

### `POST /separate/download`

上传 + 分离 + 直接**下载 ZIP**（包含所有 stem 文件）。

**表单字段**：与 `/separate` 相同。

**响应** `200 OK`
- `Content-Type: application/zip`
- `Content-Disposition: attachment; filename="<stem>_separated.zip"`
- `X-Cached: true|false` —— 是否来自缓存
- 响应体为 ZIP 二进制；ZIP 内每个文件名为 `<stem>.<output_format>`

**示例（curl）**
```bash
curl -X POST http://127.0.0.1:83/separate/download \
  -F "file=@song.mp3" \
  -F "ensemble_preset=karaoke" \
  -F "output_format=MP3" \
  --max-time 300 \
  -o song_separated.zip
```

**示例（Python）**
```python
import requests
with open("song.mp3", "rb") as f:
    r = requests.post(
        "http://127.0.0.1:83/separate/download",
        files={"file": f},
        data={"ensemble_preset": "karaoke", "output_format": "MP3"},
        timeout=300,
    )
with open("song_separated.zip", "wb") as out:
    out.write(r.content)
```

---

### `POST /separate_async`

异步版的 `/separate`：上传文件后立即入队并返回 `task_id`，分离结果以 JSON 形式保存在服务端任务表中，客户端通过 `GET /tasks/{task_id}` 轮询。

**Content-Type**：`multipart/form-data`，**表单字段与 `/separate` 完全相同**。

**响应** `200 OK`
```json
{
  "task_id": "386f3bcb6d1c411881c91c6a1efbf4dd",
  "state": "queued",
  "status_url": "/tasks/386f3bcb6d1c411881c91c6a1efbf4dd"
}
```

`task_id` 为 32 位 hex 字符串。**任务结果在内存中保存 `AS_ASYNC_TTL` 秒（默认 3600）后自动清理**，请及时取走结果。

**示例（curl）**
```bash
curl -X POST http://127.0.0.1:83/separate_async \
  -F "file=@song.mp4" -F "ensemble_preset=vocal_balanced"
# → {"task_id":"386f...dd","state":"queued","status_url":"/tasks/386f...dd"}
```

---

### `POST /separate/download_async`

异步版的 `/separate/download`：与 `separate_async` 相同的提交语义，但分离完成后服务端**保留 ZIP 二进制**供后续 `GET /tasks/{task_id}/download` 拉取。

**Content-Type**：`multipart/form-data`，**表单字段与 `/separate/download` 完全相同**。

**响应** `200 OK`
```json
{
  "task_id": "830449fe749b467cb43b5f3cf8cca789",
  "state": "queued",
  "status_url": "/tasks/830449fe749b467cb43b5f3cf8cca789",
  "download_url": "/tasks/830449fe749b467cb43b5f3cf8cca789/download"
}
```

---

### `GET /tasks/{task_id}`

查询异步任务状态。done 时**直接内嵌 result**（json 模式）或 result 摘要（zip 模式，含 download_url 与 size_bytes）。

**响应** `200 OK`

进行中：
```json
{
  "task_id": "386f3bcb6d1c411881c91c6a1efbf4dd",
  "state": "running",
  "mode": "json",
  "input_file": "song.mp4",
  "input_size_mb": 17.16,
  "created_at": 1778118351.391,
  "started_at": 1778118351.391,
  "finished_at": null,
  "error": null
}
```

JSON 模式 done：
```json
{
  "task_id": "386f...",
  "state": "done",
  "mode": "json",
  "input_file": "song.mp4",
  "input_size_mb": 17.16,
  "created_at": 1778118351.391,
  "started_at": 1778118351.391,
  "finished_at": 1778118362.064,
  "error": null,
  "result": {
    "status": "ok",
    "duration_seconds": 10.64,
    "input_file": "song.mp4",
    "input_size_mb": 17.16,
    "preset": "vocal_balanced",
    "output_format": "WAV",
    "stems": [
      "input_8874992c630d_(Instrumental)_preset_vocal_balanced",
      "input_8874992c630d_(Vocals)_preset_vocal_balanced"
    ],
    "cached": false
  }
}
```

ZIP 模式 done：
```json
{
  "task_id": "830449...",
  "state": "done",
  "mode": "zip",
  ...
  "result": {
    "download_url": "/tasks/830449.../download",
    "size_bytes": 4031230
  }
}
```

失败：
```json
{ "task_id": "...", "state": "failed", "error": "...exception message...", ... }
```

| `state` | 含义 |
|---|---|
| `queued` | 已收下、还未开始（即将拿 GPU 锁） |
| `running` | 正在 GPU 上分离 |
| `done` | 完成，可取结果 |
| `failed` | 异常失败，原因见 `error` |

| HTTP 码 | 触发条件 |
|---|---|
| `404 Not Found` | task_id 不存在或已过 TTL 被清理 |

**轮询频率建议**：**1–3 s** 一次。同步路由的处理时延通常 5–60 s，过密轮询无意义。

---

### `GET /tasks/{task_id}/download`

下载 zip 模式异步任务的 ZIP 文件。仅 `mode == "zip"` 且 `state == "done"` 时有效。

**响应** `200 OK`
- `Content-Type: application/zip`
- `Content-Disposition: attachment; filename="<stem>_separated.zip"`
- 响应体为 ZIP 二进制；ZIP 内每个文件名为 `<stem>.<output_format>`

| HTTP 码 | 触发条件 |
|---|---|
| `404` | task_id 不存在 |
| `400` | task 是 json 模式（应该用 `GET /tasks/{id}` 拿结果） |
| `409` | task 还在 `queued` 或 `running` |
| `500` | task `failed`，响应 `detail` 含错误信息 |
| `410` | 结果已被 TTL 清理 |

---

## 异步完整用例

### Python — JSON 模式

```python
import time, requests

API = "http://127.0.0.1:83"

# 1. 提交
with open("song.mp4", "rb") as f:
    r = requests.post(f"{API}/separate_async", files={"file": f},
                      data={"ensemble_preset": "vocal_balanced"})
task = r.json()
task_id = task["task_id"]
print("submitted:", task_id)

# 2. 轮询
while True:
    s = requests.get(f"{API}/tasks/{task_id}").json()
    print(s["state"])
    if s["state"] in ("done", "failed"):
        break
    time.sleep(1)

# 3. 取结果
if s["state"] == "done":
    print("stems:", s["result"]["stems"])
    print("duration_seconds:", s["result"]["duration_seconds"])
else:
    raise RuntimeError(s["error"])
```

### Python — ZIP 模式

```python
import time, requests

API = "http://127.0.0.1:83"

with open("song.mp4", "rb") as f:
    task = requests.post(f"{API}/separate/download_async", files={"file": f},
                         data={"ensemble_preset": "karaoke",
                               "output_format": "MP3"}).json()
task_id = task["task_id"]

while True:
    s = requests.get(f"{API}/tasks/{task_id}").json()
    if s["state"] in ("done", "failed"):
        break
    time.sleep(2)

if s["state"] == "failed":
    raise RuntimeError(s["error"])

# 直接走任务给的 download_url
zip_resp = requests.get(API + s["result"]["download_url"])
with open("song_separated.zip", "wb") as out:
    out.write(zip_resp.content)
print(f"saved {len(zip_resp.content)} bytes")
```

### curl — JSON 模式

```bash
TID=$(curl -s -X POST http://127.0.0.1:83/separate_async \
  -F "file=@song.mp4" -F "ensemble_preset=vocal_balanced" \
  | jq -r .task_id)

while :; do
  STATE=$(curl -s http://127.0.0.1:83/tasks/$TID | jq -r .state)
  echo $STATE
  [ "$STATE" = "done" ] || [ "$STATE" = "failed" ] && break
  sleep 1
done

curl -s http://127.0.0.1:83/tasks/$TID | jq .result
```

### curl — ZIP 模式

```bash
TID=$(curl -s -X POST http://127.0.0.1:83/separate/download_async \
  -F "file=@song.mp4" -F "ensemble_preset=karaoke" -F "output_format=MP3" \
  | jq -r .task_id)

while :; do
  STATE=$(curl -s http://127.0.0.1:83/tasks/$TID | jq -r .state)
  [ "$STATE" = "done" ] && break
  [ "$STATE" = "failed" ] && { echo failed; exit 1; }
  sleep 2
done

curl -s -o song_separated.zip http://127.0.0.1:83/tasks/$TID/download
```

---

## 错误码

| HTTP 码 | 触发条件 |
|---|---|
| `400 Bad Request` | `output_format` 不在 `WAV/FLAC/MP3/OGG/M4A` 集合；对 json 模式任务调下载接口 |
| `404 Not Found` | 任务 `task_id` 不存在或已过 TTL 被清理 |
| `409 Conflict` | 任务尚未完成，过早调下载接口 |
| `410 Gone` | 任务结果已被 TTL 清理 |
| `500` | 任务执行失败（`detail` 含异常信息）；模型加载失败、解码失败、显存不足等 |

> 队列长度超过 GPU 处理能力时**不会**返回错误：同步路由会阻塞客户端连接直到客户端 timeout，异步路由立即接受任务并入队等待。

---

## 性能基准

**4070 Ti Super 16GB + `vocal_balanced` 集成预设 + `output_format=WAV`**

| 输入 | 服务端 `duration_seconds` | 客户端总耗时 | 实时倍率 | 峰值显存 | 峰值 GPU 利用率 |
|---|---|---|---|---|---|
| 60 s 视频 | **8.8–9.9 s** | 9–10 s | **5.9–6.6×** | ~4.9 GB | 95–97% |

> 实测数据（CPU 亲和性 24/28 核、文件系统缓存已热）。
>
> 首次冷启动（模型 ckpt 未在 OS 文件缓存中）会包含 ~3.5 GB 的磁盘 IO，首请求耗时可能高达 100s+。第二请求开始稳定。

---

## 部署快查

### 环境变量
| 变量 | 默认 | 说明 |
|---|---|---|
| `AS_PORT` | `80` | 监听端口 |
| `AS_GPU_FRACTION` | `0.9` | PyTorch 进程显存上限比例（不影响 onnxruntime） |
| `AS_DEFAULT_PRESET` | `vocal_balanced` | 默认集成预设 |
| `AS_CACHE_TTL` | `3600` | 缓存 TTL（秒） |
| `AS_ASYNC_TTL` | `3600` | 异步任务结果保留时长（秒，从 `finished_at` 起算） |
| `AS_CPU_CORES` | `cpu_count // 2` | CPU 亲和性核数 |
| `AS_MODEL_DIR` | `./models` | 模型目录 |
| `AS_OUTPUT_DIR` | `./output` | 中间输出目录 |
| `AS_LOG_DIR` | `./logs` | 日志目录 |
| `AS_CACHE_DIR` | `./cache` | 缓存目录（目前未使用） |

### 本机生产部署（systemd）
- 单元名：`audio-separator.service`
- 端口：`83`（通过 `AmbientCapabilities=CAP_NET_BIND_SERVICE` 绑定 <1024 端口）
- 工作目录：`/home/cjh/code/AudioSeparator`
- 用户：`cjh`
- 启动：`systemctl start audio-separator`
- 日志：`journalctl -u audio-separator -f`

详见 [`deploy.md`](../deploy.md) 与 [`localserver.md`](../localserver.md)。
