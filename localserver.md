# LocalServer 部署信息

## 服务器

- **IP**: `172.30.254.14`
- **登录用户**: `root`
- **SSH Key**: `C:\Users\admin\.ssh\CC.pem`

## 服务

### AudioSeparator

- **仓库**: https://github.com/jinghuaswsx/AudioSeparator
- **端口**: 80 (内部端口)
- **部署路径**: `/opt/AudioSeparator`
- **Systemd 单元**: `audio-separator.service`
- **GPU 配额**: 40% (6.4GB)
- **模型文件**: `/opt/AudioSeparator/models/`
- **日志**: `/opt/AudioSeparator/logs/`

### 启动命令

```bash
cd /opt/AudioSeparator
source venv/bin/activate
export AS_PORT=80
export AS_GPU_FRACTION=0.4
python api_server.py
```

---

## 旧远程服务器（已弃用）

- **IP**: `14.103.220.208`
- **登录用户**: `root`
- **SSH Key**: `C:\Users\admin\.ssh\openclaw-noobird.pem`
