# 🚀 Openworld Free IPv6 VPS 到期預警守門（Watchdog）

> **⚠️ 2026-09 現況**：站方已將登入改爲 Clerk、續期驗證碼改爲 WebSocket 互動式真人驗證
> （puzzle/rotate/key/odd/match 多階段 + 行爲檢測）。**自動突破驗證碼屬繞過反自動化，本項目明確不做**。
> 本腳本此後定位為「到期預警守門」：Cookie 注入登入 → 偵測 VPS 狀態與剩餘天數 →
> 進入續期窗口（剩 ≤5 天）時 Telegram 通知人工到面板撳「Renew free」完成真人驗證。
> 實際運行實例為 NAS 持久區容器 cron（每日 09:00 北京），本倉庫 workflow 保留作手動備用。

腳本本體只有一個檔案 `apprenew.py`，跑批外殼（重試、結果分類、通知、報告排版）
統一走 [renew-kit](https://github.com/jardanlau2020/renew-kit) 的 composite action。

---

## 每輪做什麼

1. 注入 `OPENWORLD_COOKIES`（Clerk 的 `__session` 是短效 JWT，注入前先刷一次）。
2. 確認會話真的有效——**三態**判斷，不再把上游 502 當成「會話有效」：
   * `ok`：進了 `/dashboard` 或 `/vps`；
   * `cookie_dead`：被 302 到 `/login`；
   * `upstream_down`：面板 / Cloudflare 返 5xx，本輪拿不到真實狀態。
3. 從面板自動找出帳號下所有 VPS 實例頁。
4. 逐台讀狀態：`stopped` 就自動點 `Start` 並輪詢（最多 3 次 × 60 秒）。
5. 讀剩餘天數，按下面的節律決定「標紅 / 發通知 / 靜默」。

---

## 結果分類

| 情形 | Outcome | job | 發 TG |
|---|---|:---:|:---:|
| 剩 > 閾值（默認 5 天） | `SKIPPED` | 🟢 | ❌ |
| 剩 == 閾值（進窗口當日） | `FAILED` | 🚨 | ✅ 帶續期按鈕 |
| 剩 == 閾值-1（窗口中間日） | `FAILED` | 🚨 | ❌ **故意靜默** |
| 剩 ≤ 3 天 | `FAILED` | 🚨 | ✅ 升級為 🚨 |
| 面板 / CF 返 5xx、隧道故障 | `TRANSIENT` | 🟢 | ✅ 說明上游故障 |
| 被彈回登入頁（Cookie 失效） | `FAILED` | 🚨 | ✅ |
| 面板正常但確實無實例 | `FAILED` | 🚨 | ✅ |
| 實例頁 404（機器可能已註銷） | `FAILED` | 🚨 | ✅ |
| 未配置任何認證 | `FAILED` | 🚨 | ✅ |

**關鍵點**：`標紅` 與 `發通知` 是兩件事。窗口中間日會標紅但不發消息，
避免「每日跑批」變成「每日轟炸」。

上游 5xx **不算腳本失敗**——這是 renew-kit 的核心口徑，也是本倉庫遷移的主要動機：
2026-09-30 run #82（`Cloudflare Tunnel error`）與 2026-10-02 run #85（`502: Bad gateway`）
都被舊代碼判成「面板搵唔到任何 VPS 實例」，把上游故障說成帳號沒機器，白標紅 + 發錯告警。

---

## 通知節律與 `.renew-handled`

腳本跑到底（無論 exit 0 還是 1）都會在 cwd 寫一個 `.renew-handled` 標記。
workflow 的「兜底通知」步驟只在 **沒有這個標記** 時才發消息。

為什麼需要它：

* 窗口中間日是**故意靜默**的，那天 job 會標紅。如果兜底步驟寫成「`failure()` 就發」，
  靜默日就會被兜底通知破功。
* 但兜底又不能直接關掉——腳本連 `import` 都沒跑起來時（pip / playwright 裝掛），
  就徹底沒人知道了。

所以 action 內置的 `notify-on-failure` 設為 `'false'`，兜底交給 workflow 裡
那個先檢查標記的步驟。

---

## 環境變數 / Secrets

| 名稱 | 必填 | 說明 |
| :--- | :---: | :--- |
| `OPENWORLD_COOKIES` | ✅ | 面板 Cookie 頭（`k=v; k2=v2` 形式）。**首選認證方式**。 |
| `DISCORD_TOKEN` | ❌ | 2026-09 起 Discord OAuth 已失效，僅作回退，實際不可用。 |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token。不配就只打印日誌、不發通知。 |
| `TG_CHAT_ID` | ❌ | 接收通知的 chat id。 |
| `RENEW_THRESHOLD_DAYS` | ❌ | 進窗口的天數閾值，默認 `5`。 |
| `ACCOUNT_LABEL` | ❌ | 帳號標籤，會拼進報告名（如 `Openworld（主號）`）。 |
| `SCREENSHOT_DIR` | ❌ | 截圖落地目錄，默認 `.`。 |
| `HEADLESS` | ❌ | 默認 `true`。調試時設 `false`。 |
| `DRY_RUN` | ❌ | `1` = 照跑一遍但不發 TG，消息原文打進日誌。 |

> [!NOTE]
> 本倉庫**不需要代理**。Cookie 注入從 runner IP 直接成功（見 run #84 日誌），
> 所以不需要 `NODE_LINK` / sing-box 那一套。

---

## 怎麼跑

### GitHub Actions（手動備用）

`Actions` ➔ `Openworld VPS Renew` ➔ `Run workflow`。
`dry_run` 勾上就是演練：照跑一遍、跳過 TG，但把**本該發送的消息原文與內聯按鈕**
原樣打進日誌——否則「演練通過」只能證明流程沒崩，證明不了消息對不對。

定時任務是 `0 1 * * *`（UTC 01:00 = 北京 09:00）。

### 本地 / NAS cron

```bash
pip install playwright requests Pillow numpy
playwright install --with-deps chromium

DRY_RUN=1 xvfb-run --auto-servernum python3 apprenew.py
```

---

## 離線驗證

改動分類邏輯時，真站點沒法穩定復現各種上游狀態（總不能等上游真掛），
用 `.verify/verify_openworld.py` 在假 Playwright 上把每個分支都走一遍：

```bash
python .verify/verify_openworld.py            # 全跑（224 項斷言）
python .verify/verify_openworld.py B          # 只跑場景矩陣
```

* `[A]` 純函數：`_slug` / `_target_name` / `upstream_failure` / `_esc`
* `[B]` 場景矩陣：假頁面 + 假 Playwright，跑真 `main()`，看退出碼 / 報告 / 通知
* `[C]` 靜態接線：源碼與 workflow 的口徑一致性（含 AST 與 YAML 解析）
* `[D]` 子進程：真跑一次 `python apprenew.py`，確認進程級行為

`RENEWKIT_ROOT` 環境變數可以指定 renew-kit 的本地檢出位置，否則向上三層自動找。

---

## ⚠️ 免責聲明

- 本腳本僅供個人自動化運維及 Python 自動化學習交流使用。
- 請遵守 Openworld 平台的服務條款 (Terms of Service)，作者不對任何使用不當導致的帳號問題負責。

---

<details>
<summary>以下爲歷史文檔（Discord OAuth + GIF 验证码时代，已失效，仅存档）</summary>

基于 GitHub Actions 的 **Openworld Free IPv6 VPS** 全自动续期工具。采用 Playwright 自动化技术 + 智能 Discord OAuth 授权 + 多帧 GIF 动态验证码解析，实现无须人工干预的永久续期。

## 🌟 功能特性

- 🔑 **Discord OAuth 免干预登录**：利用账号的 `DISCORD_TOKEN` 向 Discord API 提交直接授权，跳过复杂的网页交互。
- 🧩 **多帧 GIF 算式验证码识别**：
  - 自动在浏览器上下文中获取 `blob:` 类型的多帧 GIF 动态验证码。
  - **拆帧 + 分区切割**：提取 GIF 的所有帧，将画面切割为左半区（数字A）、中区（运算符）、右半区（数字B）。
  - **模糊映射与跨帧投票**：清洗字符并映射误识别符号，利用跨帧概率统计得出高准确度的算式并自动计算结果。
- ⏱️ **智能天数检测**：自动解析面板当前的剩余到期天数，仅当剩余时间 `<= 5 天` 时才触发续期，避免无谓请求。
- 📢 **Telegram 结果通知**：可选配置 Telegram Bot，续期成功或失败时自动推送最新状态。
- 📸 **自动保存验证码GIF**：自动保存验证码GIF，在 GitHub Actions 中保存为 Artifacts 便于排查。

## 🔍 如何获取 Discord Token（已失效，仅存档）

1. 使用电脑浏览器打开 [Discord 网页版](https://discord.com/app) 并登录你的账号。
2. 按 `F12`（或 `Ctrl + Shift + I`）打开开发者工具。
3. 切换到 **网络 (Network)** 标签页。
4. 在 Discord 中点击任意频道，触发 API 请求。
5. 在网络请求列表中找到 `discord.com/api/...` 的请求。
6. 在右侧 **请求标头 (Request Headers)** 中找到 `Authorization` 字段，该字段对应的长字符串即为 **DISCORD_TOKEN**。

> ⚠️ 2026-09 起站方改用 Clerk 登入，此流程已不可用。代码里 `login_with_discord_token()`
> 与整段 GIF/OCR 识别逻辑（`try_renew_captcha` 等）均無呼叫者，保留僅為存檔。

</details>
