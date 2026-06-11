# Chanlun Analysis

A Flask + Vue stock analysis application. The backend uses akshare and performs all calculations. The frontend uses Vue 3, Element Plus, and Chart.js for interaction and visualization.

## Features

- Stock code/name search.
- Automatic Shanghai Composite analysis and independent stock analysis.
- Daily Chanlun analysis for the latest year: merged K-lines, strokes, centers, buy/sell points, and future anchors.
- Intraday short-term analysis on 30-minute, 15-minute, and 5-minute K-lines using RSI, EMA, and MACD.
- Volume bars, horizontal chart window slider, hover market data, and signal-row click focusing.
- All calculation logic runs on the backend. The frontend only displays backend results.

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
