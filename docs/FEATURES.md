# 定制功能说明（本 fork）

基于上游 `miaojuncn/Trae2api-cn`，本分支新增两个功能。

## 功能一：每日随机签到时间表

多账号不再同时（或固定时刻）签到。每天为每个账号在配置窗口内分配一个随机签到时间点，任意两个账号的时间点至少间隔最小间隔秒数，模拟自然分散的使用行为。

### 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TRAE_CHECKIN_WINDOW_START` | `06:00` | 随机签到窗口开始（北京时间 HH:MM） |
| `TRAE_CHECKIN_WINDOW_END` | `21:00` | 随机签到窗口结束 |
| `TRAE_CHECKIN_MIN_GAP_SECONDS` | `600` | 任意两个账号签到时间点的最小间隔 |

- 当天计划存于 `data/checkin_plan.json`（与 `accounts.json` 同目录，Docker 卷持久化）。容器重启后继续执行当天剩余计划，**不会重新随机**；跨天自动生成新计划。
- 窗口装不下 `n` 个账号 × 最小间隔时，自动压缩为 `窗口秒数/(n-1)` 并写 warning 日志。
- 执行复用现有 `_claim_checkin_account`：9074 退避、设备 ID 轮换、已签到跳过全部保持不变；处于 9074 冷却的账号保留为待执行，由既有后台重试循环接管。
- 与原有「自动签到」（固定 HH:MM 全量跑）相互独立，两者可同时开启。
- 面板「自动签到」卡片内可展开「今日随机签到计划」查看各账号计划时间与执行状态；API：`GET /api/checkin/schedule`。

### 验证步骤

1. 多账号运行，查看 `data/checkin_plan.json`：每条 `entries[].at` 落在窗口内，两两差 ≥ 有效间隔。
2. 重启容器，计划文件内容不变（同一 date 直接复用）。
3. 面板展开「今日随机签到计划」，或 `curl -H "Authorization: Bearer $TRAEROUTER_API_KEY" localhost:5006/api/checkin/schedule`。
4. 到点后对应账号 `accounts.json` 的 `checkin.checked_in` 变 true；计划条目 `result=ok`。

## 功能二：按账号独立出口代理

每个账号可单独配置上游出口代理（签到、模型请求、积分查询、token 刷新均走该代理），让不同账号的请求来自不同出口 IP；未配置代理的账号保持原有直连行为不变。

### 使用

- 面板「账号列表」每行新增地球图标按钮 → 展开代理编辑行，输入代理地址保存；「清除」恢复直连。
- API：`POST /api/accounts/proxy`，body `{"account_id": "...", "proxy": "http://user:pass@host:port"}`，`proxy` 传空字符串即清除。
- 支持 `http://`、`https://`、`socks5://`、`socks5h://`，均可带 `user:pass@` 认证。
- 立即生效，无需重启；保存到 `accounts.json` 对应账号记录的 `proxy` 字段。
- `/api/accounts` 与面板只显示脱敏代理（`http://***@host:port`），不泄露凭据。
- socks5 依赖 `httpx[socks]`（已加入 requirements.txt）。

### 验证步骤

1. 面板给账号 A 配置 `http://<代理地址>:<端口>`，账号 B 留空。
2. 触发 A 的签到（或模型请求），在代理服务日志可见请求来自 A 的账号流量；B 仍直连。
3. `curl .../api/accounts` 确认返回的 `proxy` 字段已脱敏、无密码。
4. 清除 A 的代理后再次请求，恢复直连。

## 注意事项

- `data/checkin_plan.json` 由 relay 自动管理，手动删除后下一轮（≤30s）会重新生成当天计划。
- 代理地址错误（不可达）会导致该账号上游请求失败，日志可见连接错误；不影响其他账号。
