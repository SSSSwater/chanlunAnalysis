# 公网访问（cloudflared 隧道 + 静态构建）

项目前端通过 Cloudflare 隧道对外提供，域名：**https://jiaren.aqhxx.top**

## 架构

```
公网用户 ──> https://jiaren.aqhxx.top
                 │  cloudflared 隧道 (Windows 服务 CloudflaredJiaren)
                 ├── /api/*  ──> http://127.0.0.1:5000   (Flask 后端)
                 └── 静态站点 ──> Node 静态服务器 :5173 (frontend/dist)
```

## 为什么"慢"：开发服务器直接对公网是不行的

最初隧道指向 Vite **dev server**（5173）。dev 模式没有打包压缩：
entry 页按需拆分几十上百个模块、Element Plus 未压缩 4MB+、每个资源都要重新
编译/转换，全部经隧道传输 → 首屏极慢，且页面里唯一的真·远程请求是
Cloudflare 自动注入的 `static.cloudflareinsights.com/beacon.min.js`（统计脚本，
国内访问常超时，可去 Cloudflare Dashboard → Web Analytics 关闭以消除）。

**修复：改用生产构建（vite build）+ 静态文件服务。**

## 静态化改造要点

1. **构建**：`cd frontend && npm run build`。
2. **分包**：`vite.config.js` 用 `manualChunks` 把第三方库拆成
   vendor-vue / vendor-element / vendor-chart / vendor-ui + 应用入口，
   避免单个 1.3MB 大文件，多文件并行加载、可长期缓存。
   - 结果：入口 74KB、Element Plus 921KB、Chart.js 171KB、Vue 80KB、UI 48KB
   - Cloudflare 边缘自动压缩：最大的 element 270KB → 243KB (Brotli)，实测
     公网加载 ~0.7s，命中边缘缓存后 <0.3s。
3. **静态服务器**：`frontend/serve-dist.mjs`（纯 Node，无依赖）。
   - 监听 `127.0.0.1:5173`，服务 `frontend/dist`
   - Brotli/gzip 动态压缩；`/assets/*` 返回
     `Cache-Control: public, max-age=31536000, immutable`（文件名含内容 hash，
     可永久缓存）；HTML 返回 `no-cache` 始终取最新
   - **关键**：缺失的 `/assets/*` 请求返回 404（不能做 SPA fallback 假 200，
     否则 Cloudflare 会把假 HTML 永久缓存导致白屏）。仅无扩展名的路径回退
     index.html。
4. **运行方式**：计划任务 `JiarenStaticServer`（`schtasks /Create /SC ONSTART
   /RU SYSTEM /RL HIGHEST /TR "node serve-dist.mjs"`），开机自启。
   （不能注册成 Windows 服务——node 不响应 SCM 控制协议，会报 1053。）

## 关键信息

- 隧道名称：`jiaren`，ID：`86a19e27-1397-40ff-a407-3fb134b69cc5`
- DNS：`jiaren.aqhxx.top`（CNAME，Cloudflare 托管）
- 隧道配置：`C:\Users\mizuss\.cloudflared\jiaren.yml`（/api/* → 5000，其余 → 5173）
- 账户：Cloudflare 账户已授权（`~/.cloudflared/cert.pem`）

## 管理命令（需管理员权限）

```powershell
# 隧道服务
Start-Service CloudflaredJiaren / Stop-Service CloudflaredJiaren

# 静态服务器计划任务：停止旧进程、重新拉起
Get-CimInstance Win32_Process -Filter "Name='node.exe'" | ? {$_.CommandLine -match 'serve-dist'} | % { Stop-Process -Id $_.ProcessId -Force }
schtasks /End  /TN JiarenStaticServer
schtasks /Run  /TN JiarenStaticServer

# 重新构建后手动刷新（新 hash 会自动缓存，旧文件失效）
cd frontend; npm run build
```

## 代码改动汇总

1. `frontend/src/api.js` — 公网域名（非 localhost/127.0.0.1）访问时 API 自动
   改用同源 `/api/*`；本地开发仍是 `http://127.0.0.1:5000`。
2. `frontend/vite.config.js` — `server.allowedHosts` 加 `jiaren.aqhxx.top`
   （dev 模式需要）；`build.rollupOptions.output.manualChunks` 分包。
3. `frontend/serve-dist.mjs` — Node 静态服务器（构建产物托管）。

## 前置条件

- 后端 Flask 运行在 `127.0.0.1:5000`（`start.bat` 或手动）。
- 前端生产目录 `frontend/dist` 存在且为最新构建。
- 公网页面加载快慢还取决于后端接口返回（/api/stocks 一次约 1.6MB），
  与静态资源无关。


## 主题系统与深色模式（新增）

- **设计令牌**：`frontend/src/styles.css` 顶部 `:root`（浅色）+ `html.dark`（深色）
  定义全套 token：`--app-bg/--panel-bg/--text-*/--brand/--up/--down/
  --radius-*/--shadow-*/` 及 Element Plus 主色变量（`--el-color-primary` 等）。
  改一处 `--brand` 即可整体换肤。
- **深色模式**：Element Plus `dark` class + `element-plus/theme-chalk/dark/css-vars.css`。
  切换按钮在 header（Sun/Moon 图标），状态存 `localStorage['jiaren-theme']`，
  `index.html` 内联脚本 + `main.js` 读取，避免刷新闪烁；默认跟随系统偏好。
- **Tailwind 渐进引入**：`tailwind.config.js`（preflight 关闭，不干扰 Element Plus
  与既有样式）+ `postcss.config.js` + 入口 `src/tailwind.css`。工具类随用随生成，
  已用于 header 区域（`flex items-center gap-2` 等），组件仍用 Element Plus。
- **K 线图美化**（`AnalysisPanel.vue`）：
  - 所有图表颜色改为从 CSS 变量实时读取（`themeColors()`，1200ms 缓存失效并在
    html.dark 变化时重绘），深色模式自动切换配色。
  - 新增十字光标插件（虚线跟随鼠标，无新依赖）。
  - 成交量柱改为垂直渐变填充、圆角；网格改为虚线；涨跌色用 A股 语义
    （红涨 `--up` / 绿跌 `--down`）。
  - 图例移到底部并跟随主题。
- 源码改动文件：`frontend/src/styles.css`、`frontend/src/main.js`、
  `frontend/index.html`、`frontend/src/App.vue`、
  `frontend/src/components/AnalysisPanel.vue`、`tailwind.config.js`、
  `postcss.config.js`、`frontend/src/tailwind.css`。

## 深色模式遗漏修复（重要）

- 根因：首批 token 化只覆盖了 `background:#fff` / `var()` 两态，
  大量容器实际写成 `rgba(255,255,255,0.9x)` 或渐变（`linear-gradient(180deg,#fbfdff,...)`），
  这些未走变量，深色下一律保持白色。
- 修复：①全部 `rgba(255,255,255,0.x)` 背景替换为 `var(--panel-bg)`；②在
  styles.css 深色层新增按选择器的覆盖（main-sidebar / portfolio-panel / signals-section /
  detail-wide-section / discipline-* / golden-meta-card / fundamental-card / chart-frame /
  range-panel / 警示条 / 表格头 / 边框 / el-button 主色按钮 等）；③补两条兜底
  （chart-frame .el-empty / range-placeholder-track）。
- 审计结论：base CSS 中所有硬编码背景选择器均已有 `html.dark` 覆盖或用变量，
  无遗漏。

## 服务持久化（本次新增）

三块服务均开机自启：
1. 隧道：Windows 服务 `CloudflaredJiaren`（cloudflared tunnel run）
2. 前端静态：计划任务 `JiarenStaticServer`（node serve-dist.mjs, :5173）
3. 后端：计划任务 `ChanlunBackend`
   （python run_prod.py —— waitress 生产 WSGI，禁 debug reloader，绑 127.0.0.1:5000）
   已装 waitress 3.0.2 到 venv。

## 布局重构（顶部导航 + 左侧分页切换）

- **顶部导航栏**（`.top-nav`，grid-area topnav）：
  左侧品牌 = logo + 完整标题"事到如今，最对不起的还是家人" + "·" + 当前分页名
  （`.brand-main-title` 一行显示、超长省略；当前页名高亮品牌色）；
  中部主页面导航（首页/纪律交易/指数分析/个股分析）；
  右侧 = 深浅色切换（🌙/☀️）+ AI设置。
- **左侧导航栏**（`.main-sidebar`，grid-area sidebar）：
  保留为主页面的"分页切换"（标题"分页切换" + 四个主页面按钮）。
- **页面头部**（`.app-header`）：简化为当前页大标题 + 副标题（移除品牌图，
  避免与顶部重复）。
- 响应式：≤1080px 页签 100% 宽可横向滚动；≤560px 隐藏左侧、仅顶部导航。
- 深色模式：`.top-nav` 用深色面板底色，当前页名用亮蓝。

## 指数分析修复 + 数据源加固

- **症状**：点左侧"指数分析"进入后一直空白/不加载。
- **根因 1（前端 UX）**：进入 index 页不会自动触发 `loadIndex()`，
  `indexResult` 为 null，面板停留在空态（此前依赖旧布局里一直可见的 sub-nav）。
  修复：`watch(activePage)` 增加 index 分支——进页时若无当前指数结果则自动加载。
- **根因 2（数据源）**：东方财富 `get_kline` 对指数经常抛
  `RuntimeError: 东方财富K线无法获取`，但旧代码只在"异常"时 fallback；
  若东财静默返回空会中断。已加固 `get_index_history`：
  1. 东财失败/为空 → akshare 多源（`_fetch_index_history_ak` 内部已有 腾讯/新浪/东财
     3 个 fetcher，逐个尝试"有哪个用哪个"）
  2. 全部在线源失败 → 回退到任意过期的本地缓存（报错前兜底）
  3. 仍失败 → 报详细错误（列出各源失败原因）
- **服务**：后端以 `Start-Process` 独立进程跑 `run_prod.py`（不绑会话 job），
  开机自启计划任务 `ChanlunBackend` + 前端 `JiarenStaticServer` 均已确认。

## K线布局溢出修复（个股/指数分析页）

- **症状**：中间 K 线超出容器；左（信息）/中（图表）/右（信号）三列重叠。
- **根因**：底部区间滑块 `.range-panel .el-slider` 的 Element Plus 按钮半伸出设计
  使 `scrollWidth` 比容器宽 18px，把 grid 中间列 `min-content` 撑大 →
  13fr 轨道被迫加宽 → 与右侧 signals 列重叠，K 线区域横向溢出。
- **修复**（`styles.css`）：
  1. `.analysis-row` 网格下限放宽（info 170px / chart 0 / signals 240px），
     并 `overflow-x: clip`
  2. `.chart-area` 及三列 `min-width:0` + `overflow: hidden`（视觉裁剪，K线不画出容器）
  3. `.range-panel .el-slider / __runway / __wrapper` 设 `width:100%; min-width:0`
  4. 内部内容（toolbar/heading/hover strip）`overflow-wrap:anywhere` 防长串撑破
- **验证**（headless Chrome CDP 实测 1440×900）：三列无重叠（AB/BC 均 false）、
  整行无水平溢出、canvas 完整在图表容器内。

## 视觉美化（背景 + 玻璃拟态 + 图标点缀）

- **背景**：纯 CSS 多层径向光晕 + 细腻网格纹理（46px 网格线），浅色/深色分别
  适配；不引入外部图片，加载快、无版权、不干扰数据浏览。
- **玻璃拟态**：主要数据面板（首页卡片、分析三列、纪律面板、黄金柱/基本面卡片）
  加 `backdrop-filter: blur(10px) saturate(1.05)` + 内发光边框 + 柔和阴影。
- **顶部导航**：品牌区光晕 + 高光线；侧栏渐变面板。
- **首页图标点缀**：今日市场/主要指数/涨幅榜/成交额榜 标题前加 lucide 小图标
  （Gauge/BarChart3/TrendingUp/Coins），34px 圆角品牌色徽章。
- 验证：headless CDP 实测 4 图标渲染、无横向溢出、背景/玻璃生效。

## 端口规范（强制约定）

> ⚠️ **默认只启动两个端口：前端 5173、后端 5000。禁止启动任何其他端口。**

| 服务 | 端口 | 启动方式 | 说明 |
|---|---|---|---|
| 前端静态服务 | **5173** | `node frontend/serve-dist.mjs` | 服务 `frontend/dist`（生产构建） |
| 后端 API | **5000** | `python backend/run_prod.py` | waitress 生产 WSGI |

- **固定端口来源**：
  - `frontend/serve-dist.mjs`：`const PORT = 5173`
  - `backend/run_prod.py`：`serve(app, host="127.0.0.1", port=5000, threads=8)`
- **隧道映射**（`C:\Users\mizuss\.cloudflared\jiaren.yml`）：
  - `/api/*` → `http://127.0.0.1:5000`
  - 其余 → `http://127.0.0.1:5173`
- **禁止事项**：不要改成 4173 / 5174 / 5001 等任何其他端口；不要用
  `--port` / `PORT` 环境变量临时覆盖；本地开发（`npm run dev` 的 5173）
  也保持与生产一致，避免端口漂移导致公网 530/502。
- **开机自启**（随文件默认端口自动适配，无需单独改端口）：
  - `JiarenStaticServer`（`node serve-dist.mjs`）
  - `ChanlunBackend`（`python run_prod.py`）
  - `CloudflaredJiaren`（cloudflared 隧道服务）

> 校验命令（应仅看到 5000 与 5173，且无其他项目端口）：
> ```powershell
> Get-NetTCPConnection -State Listen -LocalPort 5000,5173 | Select-Object LocalPort
> ```
