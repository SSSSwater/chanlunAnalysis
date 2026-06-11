# Chanlun Analysis

一个前后端分离的缠论日 K 自动分析示例系统。

- 后端：Flask + akshare，负责股票搜索、近一年日 K 获取、K 线合并、分型、笔线和三类买卖点分析。
- 前端：Vue 3 + Element Plus + Chart.js，只负责交互和可视化展示。
- 数据：实时调用 akshare，不落库。

## 后端启动

```powershell
cd backend
python -m venv ..\venv
..\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

默认地址：`http://127.0.0.1:5000`

## 前端启动

```powershell
cd frontend
npm install
npm run dev
```

默认地址：`http://127.0.0.1:5173`

## 说明

三类买卖点识别采用工程化启发式规则，适合做自动标注和辅助观察，不构成投资建议。
