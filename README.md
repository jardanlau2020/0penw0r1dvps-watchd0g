# 0penw0r1dvps-watchd0g

Openworld（openworld.eu.org）免费 VPS 的自动续期守门脚本。

**本仓 2026-11-21 起整份采用上游方案**，不再是 renew-kit 架构：

| 来源 | 内容 |
| :--- | :--- |
| 上游 `18686187636/openworldvps-renew` @ `24a3ee82`（2026-10-16） | `apprenew.py` 主体：WebSocket 抓验证码帧 + opencv 求解 + 拟人化拖拽，五类验证码（puzzle / key / rotate / odd / match）全自动 |
| 本仓补丁 | token 预检、cookie 注入、Clerk 换会话、`SKIP_DISCORD`、`dump_dom`、跨轮熔断、静默节律 |

上游文件字节 54813 / 1511 行，与 `openworldvps-renew-2` 是同一 blob（`5972329b`）。

## 工作方式

```
每天 01:00 UTC（北京 09:00）
  → 登录（DISCORD_TOKEN 首选；cookie + Clerk 换会话兜底）
  → 找 VPS 实例
  → 剩余 > 5 天：静默跳过（不发通知）
  → 剩余 ≤ 5 天：点 Renew free → 抓 WS 帧 → 算偏移/角度 → 拖拽提交
  → 成功/失败都发 Telegram
```

## Secrets

| 名称 | 必填 | 说明 |
| :--- | :---: | :--- |
| `DISCORD_TOKEN` | ✅（二选一） | Discord 用户 token。启动先打 `/users/@me` 预检：401 就是被吊销，直说。 |
| `OPENWORLD_COOKIES` | ✅（二选一） | 整行 Cookie。必须含 `sessioncookie` + `csrf_token`；带 `__session` / `__client_uat` 时会自动换新会话。 |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | ❌ | 不配就只打日志。 |
| `ACCOUNT_LABEL` | ❌ | 拼进日志/通知的账号标签。 |

## 环境变量（workflow 已配，一般不用动）

| 名称 | 默认 | 说明 |
| :--- | :--- | :--- |
| `RENEW_THRESHOLD_DAYS` | `5` | 剩余 ≤ 此值才进续期窗口。 |
| `HEADLESS` | `true` | 调试时设 `false`。 |
| `SCREENSHOT_DIR` | `./screenshots` | 截图与 DOM dump 落地目录（workflow 上传这个目录）。 |
| `CLERK_EXCHANGE_WAIT` | `90` | 等登录页自己换 `sessioncookie` 的最长秒数。 |
| `MAX_FAIL_STREAK` | `2` | 连续几轮 cron 失败就熔断（跨轮次，不是限制单轮重试）。 |
| `SKIP_DISCORD` | 关 | `1` = 跳过 Discord 授权，只走 cookie / Clerk。 |
| `DUMP_DOM` | 关 | `1` = 只抓 VPS 页真实 DOM，不点续期。 |

## 手动触发

`workflow_dispatch` 两个入参：

- `dry_run`：照跑但不发 Telegram，消息原文打日志。
- `dump_dom`：登录进 VPS 页后 dump `screenshots/dom_*.html` / `_assets.txt` / `_candidates.txt` / `dom_*.png`，**不点续期、不碰验证码**。站点改版后用它拿真实选择器。

## 熔断

连续 `MAX_FAIL_STREAK`（默认 2）轮 cron 都失败 → 停手并推一条 Telegram。计数存在 `ow_fail_streak.txt`（actions/cache 跨轮次传递）。人工处理完删掉缓存或把文件归零即可恢复。

## 验证

```bash
python .verify/verify_openworld.py
```

52 项断言，覆盖五条主路径：token 预检 / Clerk 换会话 / 检测实例 / 未到窗口静默 / 到窗口进验证码流程，外加静态接线（patchright、上游 solver 在位、人工路径已删、workflow 接线）。

## 已知限制

- 验证码结构取自上游 2026-10-16 的实现（`#captcha_box_default` 等）。站点 2026-10-27 改版过，若 WS 抓不到帧，先跑 `dump_dom` 拿真实 DOM 再改选择器。
- 全自动解验证码 + 模拟拖拽有被站方判定为自动化的风险，这是采用上游方案时就接受的取舍。
