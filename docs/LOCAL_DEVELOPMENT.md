# 本地开发端口规范

## 固定端口

本项目本地运行只使用以下两个端口：

| 服务 | 地址 | 固定端口 |
| --- | --- | ---: |
| Flask 后端 | `http://127.0.0.1:5000` | `5000` |
| Vue 前端 | `http://127.0.0.1:5173` | `5173` |

`5173` 是开发服务器和构建产物静态服务器的统一前端端口。`5000` 是 Flask
开发/生产启动器的统一后端端口。

## 启动方式

推荐从项目根目录运行：

```powershell
.\start.bat
```

需要使用 Tushare 默认数据源时，先在同一个 PowerShell 会话中配置 Token，再运行公网启动脚本：

```powershell
$env:TUSHARE_TOKEN = "your-tushare-token"
$env:TUSHARE_HTTP_URL = "https://jiaoch.top/"
.\start-public.bat
```

Token 只通过环境变量传入，不要提交到仓库。未配置 Token 时会自动回退到其它数据源。

手动启动时也必须使用固定端口：

```powershell
# backend
python -m flask --app backend.app run --host 127.0.0.1 --port 5000 --with-threads

# frontend development server
cd frontend
npm run dev

# frontend built static server
npm run build
npm run serve
```

## 端口冲突处理

- `frontend/vite.config.js` 使用 `strictPort`，5173 被占用时应报错并处理占用进程。
- `frontend/scripts/run-fixed-vite.mjs` 会固定开发/预览端口，并忽略命令行传入的其他端口。
- `frontend/serve-dist.mjs` 固定监听 5173，不读取 `PORT` 环境变量。
- 不得通过 `--port` 或环境变量改用其他本地端口。
- 禁止使用 `4173`、`5174`、`5001` 以及其他临时替代端口。
- Render 等外部托管平台的 `$PORT` 属于平台运行时约定，不改变本地端口规范。
