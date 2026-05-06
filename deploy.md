# AudioSeparator 部署文档

## 项目信息

- **GitHub**: https://github.com/jinghuaswsx/AudioSeparator
- **服务端口**: 80（内部端口，可通过环境变量 `AS_PORT` 更改）
- **技术栈**: FastAPI + python-audio-separator + PyTorch CUDA

## 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.12+ | 实测 3.12.9 |
| CUDA | 12.x | 实测 CUDA 12.4 / 12.7 |
| NVIDIA 驱动 | >= 525 | 建议最新 |
| 显存 | >= 8GB | 建议 16GB（vocal_balanced 集成 ~4-5GB） |
| ffmpeg | 任意版本 | 用于音频编解码 |

## 快速部署

### 1. 安装 Python 3.12（如没有）

```bash
# Ubuntu
apt install python3.12 python3.12-venv

# Windows — 从 python.org 下载 3.12 安装包
# 或使用 embeddable 包（见下方 Windows 部署章节）
```

### 2. 克隆项目

```bash
git clone https://github.com/jinghuaswsx/AudioSeparator.git /opt/AudioSeparator
cd /opt/AudioSeparator
```

### 3. 创建虚拟环境

```bash
python3.12 -m venv venv
source venv/bin/activate
# Windows: venv\Scripts\activate
```

### 4. 安装依赖

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

主要依赖说明：
- `audio-separator[gpu]` — 自动安装 PyTorch CUDA 版 + onnxruntime-gpu
- `fastapi` + `uvicorn` — Web 框架
- `psutil` — CPU 亲和性和优先级控制

### 5. 配置环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AS_PORT` | `80` | 服务监听端口 |
| `AS_GPU_FRACTION` | `0.9` | GPU 显存限制比例（0.0~1.0） |
| `AS_DEFAULT_PRESET` | `vocal_balanced` | 默认模型预设 |
| `AS_CACHE_TTL` | `3600` | MD5 缓存有效期（秒） |
| `AS_MODEL_DIR` | `./models` | 模型文件目录 |
| `AS_OUTPUT_DIR` | `./output` | 临时输出目录 |
| `AS_LOG_DIR` | `./logs` | 日志目录 |

示例（与 SubtitleRemover 共存的配置）：

```bash
export AS_PORT=80
export AS_GPU_FRACTION=0.4    # 留显存给去字幕服务
export AS_DEFAULT_PRESET=vocal_balanced
```

### 6. 启动服务

```bash
python api_server.py
```

首次启动会自动下载模型（~3.5GB），包括：
- `bs_roformer_vocals_resurrection_unwa.ckpt` (195MB)
- `melband_roformer_big_beta6x.ckpt` (1.6GB)
- 以及其他预设模型

启动日志示例：

```
2026-05-05 - audio_api - INFO - Starting Audio Separator on port 80...
2026-05-05 - audio_api - INFO - GPU mem limit: 40% (6.4 GB / 16.0 GB)
2026-05-05 - audio_api - INFO - Pre-warming: vocal_balanced
2026-05-05 - audio_api - INFO - Pre-warm complete.
```

### 7. 验证

```bash
curl http://localhost/health
```

期望响应：

```json
{
  "status": "ok",
  "cuda_available": true,
  "cuda_device": "NVIDIA GeForce RTX 4070 Ti Super",
  "gpu_memory_limit": "6.4 GB (40%)",
  "default_preset": "vocal_balanced",
  "queue": {"waiting_or_active": 0, "gpu_busy": false},
  "cache": {"entries": 0, "ttl_sec": 3600}
}
```

## Systemd 服务（Ubuntu 生产环境）

创建 `/etc/systemd/system/audio-separator.service`：

```ini
[Unit]
Description=Audio Separator API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/AudioSeparator
Environment=AS_PORT=80
Environment=AS_GPU_FRACTION=0.4
Environment=AS_DEFAULT_PRESET=vocal_balanced
ExecStart=/opt/AudioSeparator/venv/bin/python /opt/AudioSeparator/api_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
systemctl daemon-reload
systemctl enable audio-separator
systemctl start audio-separator
systemctl status audio-separator
```

## Windows 部署（开发测试）

### 安装 Python 3.12

从 https://www.python.org/downloads/release/python-3129/ 下载 embeddable 包：

```bash
# 解压到 G:\audio\python312\
# 修改 python312._pth，取消 import site 的注释
# 下载 get-pip.py 并安装 pip

curl -o G:\audio\get-pip.py https://bootstrap.pypa.io/get-pip.py
G:\audio\python312\python.exe G:\audio\get-pip.py
```

### 创建 venv + 安装依赖

```bash
G:\audio\python312\python.exe -m virtualenv G:\audio\venv312
G:\audio\venv312\Scripts\pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
G:\audio\venv312\Scripts\pip install "audio-separator[gpu]" fastapi uvicorn psutil
```

### 启动

```bash
G:\audio\venv312\Scripts\python G:\audio\api_server.py
```

### 启动脚本

`start.bat`:

```bat
@echo off
cd /d G:\audio
set AS_PORT=80
set AS_GPU_FRACTION=0.4
G:\audio\venv312\Scripts\python.exe G:\audio\api_server.py
```

## Docker 部署（备选）

```dockerfile
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y ffmpeg python3.12 python3.12-venv curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python3.12 -m venv venv && \
    venv/bin/pip install --upgrade pip && \
    venv/bin/pip install -r requirements.txt

COPY api_server.py .

ENV AS_PORT=80
ENV AS_GPU_FRACTION=0.9

CMD ["venv/bin/python", "api_server.py"]
```

```bash
docker build -t audio-separator .
docker run --gpus all -p 80:80 --shm-size=8g audio-separator
```

## 首次部署完整流程（Ubuntu 新机器）

```bash
# === 1. 系统依赖 ===
apt update
apt install -y python3.12 python3.12-venv ffmpeg curl git

# === 2. 验证 GPU ===
nvidia-smi

# === 3. 部署项目 ===
git clone https://github.com/jinghuaswsx/AudioSeparator.git /opt/AudioSeparator
cd /opt/AudioSeparator
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# === 4. 配置 systemd ===
cat > /etc/systemd/system/audio-separator.service << 'EOF'
[Unit]
Description=Audio Separator API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/AudioSeparator
Environment=AS_PORT=80
Environment=AS_GPU_FRACTION=0.4
ExecStart=/opt/AudioSeparator/venv/bin/python /opt/AudioSeparator/api_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable audio-separator
systemctl start audio-separator

# === 5. 验证 ===
sleep 30  # 等待模型预热
curl http://localhost/health
curl http://localhost/presets
```

## API 调用示例

```bash
# 健康检查
curl http://localhost/health

# 列出预设
curl http://localhost/presets

# 分离音频（等待 GPU，建议 300s 超时）
curl -X POST http://localhost/separate \
  -F "file=@song.mp3" \
  -F "ensemble_preset=vocal_balanced" \
  -F "output_format=WAV"

# 下载分离结果
curl -X POST http://localhost/separate/download \
  -F "file=@song.mp3" \
  -o separated.zip
```

## 维护

### 日志

```bash
journalctl -u audio-separator -f
# 或
tail -f /opt/AudioSeparator/logs/api_server.log
```

### 更新

```bash
cd /opt/AudioSeparator
git pull
systemctl restart audio-separator
```

### 查看缓存状态

```bash
curl http://localhost/queue
# → {"waiting_or_active":0, "gpu_busy":false, "cache_entries":5}
```

## 性能基准

### 4070 Ti Super 16GB — vocal_balanced 预设

| 音频时长 | 单次耗时 | 实时倍率 |
|---------|:-------:|:--------:|
| 25 秒 | ~9s | ~2.8x |
| 1 分钟 | ~17s | ~3.5x |
| 5 分钟 | ~80s | ~3.8x |

### 缓存命中

MD5 缓存 TTL 1 小时，相同文件 + 相同参数返回时间：**<10ms**。

## 故障排查

### 服务无法启动

```bash
# 检查日志
journalctl -u audio-separator --no-pager -n 50

# 检查 CUDA
python -c "import torch; print(torch.cuda.is_available())"
# → True

# 手动启动看错误
cd /opt/AudioSeparator
source venv/bin/activate
python api_server.py
```

### 显存不足

```bash
# 查看当前显存占用
nvidia-smi

# 降低 GPU 配额
export AS_GPU_FRACTION=0.3
```

### 模型下载失败

```bash
# 检查网络
curl -I https://github.com

# 手动下载模型
python -c "
from audio_separator.separator import Separator
sep = Separator(model_file_dir='/opt/AudioSeparator/models')
sep.load_model('model_bs_roformer_ep_317_sdr_12.9755.ckpt')
"
```

---

## 相关文档

- [localserver.md](localserver.md) — 服务器部署信息
- [README.md](README.md) — 项目概述和 API 文档
- [tests/test_api.py](tests/test_api.py) — API 测试用例
