# Chanlun Analysis

缠论股票分析系统。后端使用 Flask + akshare 获取行情并完成全部计算，前端使用 Vue 3 + Element Plus + Chart.js 展示日 K、合并 K、笔线、中枢、买卖点、未来锚点和日内短线信号。

## 功能

- 股票代码或名称搜索候选。
- 上证指数自动分析，个股独立分析。
- 近一年日 K 缠论分析：K 线合并、笔线、中枢、三类买卖点、未来潜在买卖锚点。
- 30 分钟、15 分钟、5 分钟日内短线分析：RSI、EMA、MACD 辅助判断短线买卖点。
- 图表区间滑动、成交量柱、悬停行情信息、买卖点点击定位。
- 后端完成所有计算，前端只负责交互和展示。

## 本地启动

一键启动：

```powershell
.\start.bat
```

手动启动后端：

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
python -m flask --app backend.app run --host 127.0.0.1 --port 5000 --with-threads
```

手动启动前端：

```powershell
cd frontend
npm install
npm run dev
```

默认地址：

- 前端：http://127.0.0.1:5173
- 后端：http://127.0.0.1:5000

## GitHub Pages 部署

仓库内已包含 `.github/workflows/pages.yml`，会在推送到 `main` 或 `master` 后构建 `frontend` 并发布到 GitHub Pages。

注意：GitHub Pages 只能托管静态前端，不能运行 Flask 后端，也不能直接执行 akshare。要让线上页面可用，需要把 `backend` 单独部署到支持 Python 服务的平台，例如云服务器、Render、Railway、Fly.io 或其他容器平台。

部署步骤：

1. 在 GitHub 仓库 Settings -> Pages 中，Source 选择 `GitHub Actions`。
2. 单独部署 Flask 后端，并确保后端允许 Pages 域名跨域访问。
3. 在 GitHub 仓库 Settings -> Secrets and variables -> Actions -> Variables 中新增：
   - `VITE_API_BASE_URL`
   - 值填写你的后端公网地址，例如 `https://your-api.example.com`
4. 推送代码到 GitHub，等待 `Deploy frontend to GitHub Pages` workflow 完成。

如果没有配置 `VITE_API_BASE_URL`，Pages 上的前端会仍然尝试请求默认的 `http://127.0.0.1:5000`，这只适用于本机开发。

## 说明

买卖点识别是工程化启发式分析，用于辅助观察，不构成投资建议。
