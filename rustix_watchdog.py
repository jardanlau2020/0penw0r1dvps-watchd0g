#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rustix watchdog v3：心跳檢查 + 真瀏覽器過 Mitelis 牆 + panel API 自動重啟。

流程：
  1. 讀 weather.2088x.com/__status 嘅 rustix 心跳（唔經牆）
  2. 心跳健康 → exit 0（唔掂 panel，零風險）
  3. offline 或 TEST_RESTART → 起 chromium（seleniumbase uc mode + xvfb）
     → 開 rustix.me 過 Mitelis 兩層閘 → 攞 browser cookies
     → requests 帶 cookies + ptlc_ key 打 panel API
     → 搵 server → POST power start → 75s 後複查心跳 → TG 報結果

點解用瀏覽器：panel 前面有 Mitelis DDoS-Mitigation 2.0（mit_ck_p2 cookie 閘
+ jsc-post-v3 JS PoW 虛擬機，498KB 混淆 VM）。手解 PoW 唔划算，真瀏覽器
過閘後攞 cookie 轉手 API，同 katabump 過 CF 盾同一套路。

Exit codes:
  0 = 心跳健康 / TEST 模式鏈路全通
  2 = offline 但自動重啟成功（TG 已報）
  3 = offline 但重啟鏈路失敗（TG 已報，需人手撳 Start）
  4 = 連 status API 都讀唔到
"""
import json
import os
import sys
import time

import requests

STATUS_URL = os.environ.get("STATUS_URL", "https://weather.2088x.com/__status")
STALE_LIMIT = int(os.environ.get("STALE_LIMIT", "1200"))
PTERO_KEY = os.environ.get("RUSTIX_PTERO_KEY", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
PANEL = os.environ.get("PANEL_URL", "https://rustix.me")
UUID_PREFIX = os.environ.get("RUSTIX_UUID_PREFIX", "e9fb06d1")
TEST_RESTART = os.environ.get("TEST_RESTART", "") in ("1", "true", "True")


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def tg(msg):
    print("[TG] " + msg)
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return
    try:
        requests.post(
            "https://api.telegram.org/bot" + TG_BOT_TOKEN + "/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
    except Exception as e:
        print("TG error:", e)


def get_hb_age():
    try:
        r = requests.get(STATUS_URL, timeout=30)
        d = r.json()
        p = [x for x in d.get("platforms", []) if x.get("key") == "rustix"]
        if not p:
            return None
        p = p[0]
        hb = p.get("hb")
        if hb:
            return int((time.time() * 1000 - hb) / 1000)
        return int(p.get("hbAgo", 999999))
    except Exception as e:
        print("status err:", e)
        return None


def browser_pass_gate():
    """起 chromium 過 Mitelis 閘。回傳 (cookies_dict, fail_reason)。"""
    from seleniumbase import SB

    with SB(uc=True, headless=True, xvfb=True) as sb:
        sb.driver.get(PANEL)
        sb.sleep(5)
        deadline = time.time() + 60
        ok = False
        while time.time() < deadline:
            names = {c["name"] for c in sb.driver.get_cookies()}
            if "mit_ck_p2" in names:
                ok = True
                break
            sb.sleep(2)
        sb.sleep(3)
        out = {c["name"]: c["value"] for c in sb.driver.get_cookies()}
        print("browser cookies: " + str(sorted(out.keys())))
        if not ok:
            return {}, "60s 內冇攞到 mit_ck_p2 cookie"
        return out, None


def api(s, path, method="GET", payload=None):
    """帶 key 打 panel API。回 (status, json_or_None, fail_reason)。"""
    h = {"Authorization": "Bearer " + PTERO_KEY,
         "Accept": "application/json",
         "Content-Type": "application/json"}
    r = s.request(method, PANEL + path, json=payload, headers=h, timeout=25)
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        try:
            return r.status_code, (r.json() if r.text else {}), None
        except Exception:
            return r.status_code, None, "JSON 解析失敗 HTTP " + str(r.status_code)
    if "jsc-post" in r.text or "challengeTag" in r.text:
        return r.status_code, None, "撞正 jsc-post-v3 PoW 牆（瀏覽器先過到）"
    return r.status_code, None, "非 JSON 回應 HTTP " + str(r.status_code) + "（前 120 字：" + r.text[:120] + "）"


def try_restart():
    """行成條重啟鏈。回 (ok, detail)。"""
    if not PTERO_KEY:
        return False, "冇 RUSTIX_PTERO_KEY secret"

    cookies, reason = browser_pass_gate()
    if reason:
        return False, "瀏覽器過閘失敗：" + reason

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Accept-Language": "zh-CN,zh;q=0.9"})
    for name, val in cookies.items():
        s.cookies.set(name, val, domain="rustix.me", path="/")

    st, data, reason = api(s, "/api/client")
    if reason:
        return False, "GET /api/client → " + reason
    if st != 200:
        return False, "GET /api/client → HTTP " + str(st) + ": " + json.dumps(data, ensure_ascii=False)[:150]
    servers = (data or {}).get("data", [])
    target = None
    for sv in servers:
        a = sv.get("attributes", {})
        if a.get("identifier", "").startswith(UUID_PREFIX) or UUID_PREFIX in a.get("uuid", ""):
            target = a
            break
    if not target:
        ids = [sv.get("attributes", {}).get("identifier") for sv in servers]
        return False, "server list 冇 " + UUID_PREFIX + "（搵到 " + str(ids) + "）"
    sid = target["identifier"]
    print("server: " + sid + " (" + str(target.get("name")) + ") suspended=" + str(target.get("is_suspended")))

    state = "unknown"
    st3, res, reason3 = api(s, "/api/client/servers/" + sid + "/resources")
    if reason3:
        print("resources 讀唔到（" + reason3 + "），照樣試 power")
    else:
        state = (res or {}).get("attributes", {}).get("current_state", "unknown")
        print("current_state: " + state)

    st4, body4, reason4 = api(s, "/api/client/servers/" + sid + "/power", method="POST",
                              payload={"signal": "start"})
    if reason4:
        return False, "POST power → " + reason4
    if st4 in (200, 201, 202, 204):
        return True, "power start 已發出（state=" + state + ", HTTP " + str(st4) + "）"
    det = json.dumps(body4, ensure_ascii=False)[:200] if body4 else ""
    if "already" in det.lower():
        return True, "server 已經開緊（state=" + state + "），POST 冇副作用；鏈路全通"
    return False, "POST power → HTTP " + str(st4) + ": " + det


def main():
    hb = get_hb_age()
    if hb is None:
        tg("⚠️ Rustix watchdog：連 weather.2088x.com/__status 都讀唔到，睇下 Worker 係咪死咗")
        sys.exit(4)
    print("heartbeat age: " + str(hb) + "s (limit " + str(STALE_LIMIT) + "s)")

    if hb <= STALE_LIMIT and not TEST_RESTART:
        print("✅ RUSTIX_OK")
        sys.exit(0)

    mode = "TEST" if TEST_RESTART else "OFFLINE"
    print("[" + mode + "] 行自動重啟鏈路…")
    ok, detail = try_restart()
    print("restart chain: ok=" + str(ok) + " detail=" + detail)

    if TEST_RESTART:
        if ok:
            tg("🧪 Rustix 自動重啟鏈路測試：✅ 全通\n" + detail + "\n下次熄機 GHA 會自己撳掣，唔使再等你")
            sys.exit(0)
        tg("🧪 Rustix 自動重啟鏈路測試：❌ 失敗\n" + detail + "\n繼續用「TG 叫人 → 人手撳 Start」後備")
        sys.exit(3)

    if ok:
        tg("🤖 Rustix offline（" + str(hb) + "s 無心跳）→ 已自動重啟\n" + detail + "\n等 75 秒複查心跳…")
        time.sleep(75)
        hb2 = get_hb_age()
        if hb2 is not None and hb2 <= 120:
            tg("✅ Rustix 自動重啟成功，心跳已恢復（age=" + str(hb2) + "s）")
            sys.exit(2)
        tg("⚠️ power start 已發出但心跳未返（age=" + str(hb2) + "s），可能要再等多陣，30 分鐘後 watchdog 會再查")
        sys.exit(2)

    tg("🚨 Rustix offline（" + str(hb) + "s 無心跳），自動重啟失敗：" + detail + "\n請人手去 panel 撳 Start")
    sys.exit(3)


if __name__ == "__main__":
    main()
