# 开发笔记（功能分支内维护）

## 分支
- `feat/checkin-random-schedule`：功能一 随机签到时间
- `feat/per-account-proxy`：功能二 按账号出口代理（基于功能一分支）

## 架构调研结论
- 签到 claim 全局节流：`src/main.py:15036` `_claim_checkin_throttled`，`_CHECKIN_NEXT_CLAIM_AT` 单调时钟 + `CHECKIN_INTERVAL`（`src/main.py:250`）。
- 已有「自动签到」控制台开关：每天固定 `HH:MM`（UTC+8）顺序跑全部账号，`_auto_checkin_loop` / `_auto_checkin_cycle`（`src/main.py:13492` 起），设置存 `auth._settings['auto_checkin']`。
- 账号持久化：`auth._save_accounts()` 全量重写 `data/accounts.json`，`_STORE_LOCK` 线程锁保护内存态。
- 面板账号行服务端渲染：`_web_login_html()`（`src/main.py:1692` 起），行内 JS 用 `postJSON` 调 `/api/accounts/*`。
- 上游 HTTP 出口：`trae_client` / `raw_client` 每次调用新建 `httpx.AsyncClient` / `httpx.Client`，无连接池，天然适合按调用注入 proxy。

## 决策记录

### 功能一
- 随机调度独立于现有「自动签到」开关（现有开关行为不变）；环境变量 `TRAE_CHECKIN_WINDOW_START/END` 默认 06:00/21:00 即为默认启用随机调度。
- 每日计划存独立文件 `data/checkin_plan.json`（用户确认），含 date / window / gap / entries(account_id→epoch)；启动和每天 00:00 检查，当天计划缺失则生成；重启后当天条目原样恢复，不重新随机。跨天时丢弃旧计划。
- 间隔装不下时压缩为 `window_span/(n-1)` 并 warning；随机方式：以压缩后最小间隔做带间隔的随机顺序时间点（jitter 取区间剩余空间），保证任意两点差 ≥ 有效 gap。
- 到点账号通过现有 `_claim_checkin_account` 执行，天然继承 9074 退避/设备轮换/缓存跳过逻辑，不改这些代码。
- 面板「自动签到」卡片增加「今日随机签到计划」只读表格（账号+时间），便于核对。

### 功能二
- `proxy` 存账号记录；httpx 客户端创建处统一经 `auth`-based helper 取 `get_account_record(account_id).get('proxy')`；空值走原有直连构造。
- socks5 需要 `socksio`，requirements 加 `httpx[socks]`。
- 面板行内编辑 + `/api/accounts/proxy` 端点，保存即生效（客户端每次调用新建，无缓存失效问题）；`/api/accounts` 列表只暴露脱敏 proxy 显示串。
- 新增 `docs/FEATURES.md` 汇总两功能说明与验证步骤。

## 验证记录
- `pytest tests/test_checkin_schedule.py`：9 passed（窗口边界/间隔/压缩 warning/重启复用/跨天重生成/到点触发/冷却挂起/已签到跳过/计划新增账号）。
- `pytest tests/test_account_proxy.py`：21 passed（校验/脱敏/持久化/API/httpx 出口 kwargs）。
- 全量 `pytest tests/`：1247 passed, 6 skipped（Windows 专用），无回归。
- 真实出口验证：本地 toy HTTP 代理 + 源站，直连账号 proxy 命中 0 次、配置代理账号经代理转发成功（hits origin=2/proxy=1）。
- 环境注意：本机仓库 main.py/auth.py/raw_client.py 为 CRLF，已统一 LF；测试环境 Alpine 需 `pip install fastapi 'httpx[socks]' python-dotenv uvicorn`。
