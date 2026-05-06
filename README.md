# Audio Separator

GPU 加速音频人声/伴奏分离 FastAPI 服务。基于 [python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator)。

## 架构

```
POST /separate (timeout=300s)
  → MD5 缓存检查（1h TTL）
  → asyncio.Lock 排队等 GPU
  → GPU 处理 → 写入缓存 → 返回结果
```

## 依赖

- Python 3.12+
- NVIDIA GPU（建议 8GB+ 显存）
- CUDA 12.x

## 快速安装

```bash
# 1. 创建虚拟环境
python3.12 -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动（自动下载模型 + 预热）
python api_server.py
```

服务默认运行在 `http://0.0.0.0:80`，Swagger 文档 `http://localhost/docs`。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 + GPU/队列/缓存状态 |
| `GET` | `/queue` | 队列深度速查 |
| `GET` | `/models` | 列出可用模型 |
| `GET` | `/presets` | 列出集成预设 |
| `POST` | `/separate` | 分离音频，返回 JSON 元数据 |
| `POST` | `/separate/download` | 分离并下载 ZIP |

## 调用示例

```python
import requests

API = "http://your-server-ip"

# 健康检查
r = requests.get(f"{API}/health")
print(r.json())

# 分离音频（设 300s 超时，自动排队）
with open("song.mp3", "rb") as f:
    r = requests.post(f"{API}/separate",
                      files={"file": f},
                      timeout=300)
print(r.json())
# → { "status":"ok", "duration_seconds":16.37, "stems":["...Instrumental","...Vocals"], "cached":false }

# 下载 ZIP
with open("song.mp3", "rb") as f:
    r = requests.post(f"{API}/separate/download",
                      files={"file": f},
                      timeout=300)
with open("result.zip", "wb") as f:
    f.write(r.content)
```

## 预设模型

| 预设 | 说明 |
|------|------|
| `vocal_balanced` (默认) | 人声分离最佳综合质量 |
| `vocal_clean` | 最小乐器串扰 |
| `vocal_full` | 最大人声捕捉 |
| `instrumental_clean` | 最干净伴奏 |
| `instrumental_full` | 最大乐器保留 |
| `karaoke` | 去除主唱人声 |

## 性能

4070 Ti Super 16GB + vocal_balanced 预设：

| 音频时长 | 单路 | 2路并发 | 3路并发 |
|---------|:---:|:------:|:------:|
| 1 分钟 | ~9s | ~9s/路 | ~10s/路 |
| 5 分钟 | ~35s | ~35s/路 | ~40s/路 |

## 配置

编辑 `api_server.py` 顶部：

```python
SERVICE_PORT = 80          # 监听端口
GPU_MEMORY_FRACTION = 0.5  # GPU 显存占用比例（50% 留给同机其他服务）
CACHE_TTL_SEC = 3600       # MD5 缓存有效期（1 小时）
```
