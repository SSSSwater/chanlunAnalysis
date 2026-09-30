# 事到如今，最对不起的还是家人

A Flask + Vue stock analysis application. The backend uses akshare and performs all calculations. The frontend uses Vue 3, Element Plus, and Chart.js for interaction and visualization.

## Features

- Stock code/name search.
- Automatic Shanghai Composite analysis and independent stock analysis.
- Daily price-action analysis for the latest year: market environment, key levels, setups, conditional plans, invalidation, and magnet targets.
- Intraday price-action analysis on 30-minute, 15-minute, and 5-minute K-lines using the same environment, structure, confirmation, and risk-plan vocabulary.
- Volume bars, horizontal chart window slider, hover market data, and signal-row click focusing.
- All calculation logic runs on the backend. The frontend only displays backend results.
- Discipline trading workspace based on completed daily bars, including current simulated holdings, position sizing, stops, point levels, and the transaction ledger. It is read-only and never places broker orders.

交易纪律的权威来源是 [`docs/trading-discipline-source-policy.md`](docs/trading-discipline-source-policy.md) 及其中列出的四份 PDF。项目笔记、OpenSpec、代码和前端文案只能解释或实现 PDF 规则，不能自行增加操作纪律；当前默认 A 股仅允许现金买入和已有持仓卖出，除此之外不改变四本书的价格行为规则。

## Local Development

One-click start on Windows:

```powershell
.\start.bat
```

Backend:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
python -m flask --app backend.app run --host 127.0.0.1 --port 5000 --with-threads
```

Frontend:

```powershell
cd frontend
npm install
npm run dev
```

Default URLs:

- Frontend: http://127.0.0.1:5173
- Backend: http://127.0.0.1:5000

端口规范见 [`docs/LOCAL_DEVELOPMENT.md`](docs/LOCAL_DEVELOPMENT.md)。本地开发只允许使用
前端 `5173` 和后端 `5000`，端口被占用时应先处理占用进程，不得改用 `4173`、`5174`、
`5001` 等替代端口。

## Tushare v1.3.0

行情数据默认优先使用 Tushare。请把 Token 配置为环境变量，不要将真实 Token 写入代码、日志或 Git：

```powershell
$env:TUSHARE_TOKEN = "your-tushare-token"
$env:TUSHARE_HTTP_URL = "https://jiaoch.top/"
.\start-public.bat
```

`TUSHARE_HTTP_URL` 默认为 `https://jiaoch.top/`。Tushare 不可用或未配置 Token 时，系统会按
Tushare、东方财富、腾讯、AkShare 的顺序回退；历史数据仍优先读取本地缓存。

## Local Accounts and Private Data

纪律交易首次使用时需注册本地账户。密码只以安全哈希保存；浏览器保存的登录令牌有效期为 30 天，
可从顶部账户图标退出。以下资料按账户独立保存，其他账户无法读取或修改：

- 模拟资金和持仓
- 自选观察列表
- 买入、卖出交易账本
- 纪律计划与复盘记录

行情缓存、K 线、F10 和个股/指数分析仍是共享数据，不需要登录。已有未登录版本的本地数据库在
升级后会保留原个人资料；数据库中尚无账户时，首个成功注册的账户会一次性认领这些资料。升级前可
备份 `data/chanlun.db`，认领后新注册账户从独立的默认模拟资金开始。

## Render Deployment

The repository includes `render.yaml` for a Render Blueprint with two services:

- `chanlun-analysis-api`: Flask backend Web Service
- `chanlun-analysis-web`: Vue frontend Static Site

Backend production start command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 300
```

Manual steps:

1. Sign in to Render.
2. Click `New +` -> `Blueprint`.
3. Connect the GitHub repository `SSSSwater/chanlunAnalysis`.
4. Select the root `render.yaml` and create the Blueprint.
5. Wait for both services to deploy once.
6. Copy the public URL of `chanlun-analysis-api`, for example `https://chanlun-analysis-api.onrender.com`.
7. Open `chanlun-analysis-web` -> Environment. If the frontend cannot reach the backend, set `VITE_API_BASE_URL` to the backend public URL.
8. Redeploy `chanlun-analysis-web` after changing environment variables.
9. Open the `chanlun-analysis-web` Render URL.

When `VITE_API_BASE_URL` is not set on Render, the frontend tries to infer the backend URL by replacing `-web.onrender.com` with `-api.onrender.com`. This matches the service names in `render.yaml`. If you rename either service, set `VITE_API_BASE_URL` manually.

If you create services manually instead of using Blueprint:

Backend Web Service:

- Root Directory: `backend`
- Runtime: `Python`
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 300`
- Health Check Path: `/api/health`

Frontend Static Site:

- Root Directory: `frontend`
- Build Command: `npm ci && npm run build`
- Publish Directory: `dist`
- Environment Variable: `VITE_API_BASE_URL=https://your-backend-public-url`

## GitHub Pages

The repository also includes `.github/workflows/pages.yml` for deploying the frontend to GitHub Pages.

GitHub Pages can only host the static frontend. It cannot run Flask or akshare. If using GitHub Pages, deploy the backend separately and set the repository Actions variable:

- `VITE_API_BASE_URL=https://your-backend-public-url`

Without `VITE_API_BASE_URL`, the frontend defaults to `http://127.0.0.1:5000`, which is only valid for local development.

## Disclaimer

Signal detection is heuristic analysis for observation and research only. It is not investment advice.
