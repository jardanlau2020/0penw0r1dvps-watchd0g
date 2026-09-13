#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rustix watchdog v2：心跳檢查 + Pterodactyl client API 自動重啟。

流程：
  1. 讀 weather.2088x.com/__status 嘅 rustix 心跳
  2. 心跳健康 → exit 0（唔掂 panel，零風險）
  3. offline → 過 Mitelis gate（mit_ck_p2 cookie）→ 帶 ptlc_ key 打 panel API
     → 搵 server → POST power start → 等 75s → 複查心跳 → TG 報結果
  4. TEST_RESTART=1 → 唔理心跳，強制行重啟鏈路（測試用；server 開緊時
     POST start 係 docker no-op，冇副作用）

Exit codes:
  0 = 心跳健康 / TEST 模式鏈路全通
  2 = offline 但自動重啟成功（TG 已報）
  3 = offline 但自動重啟鏈路失敗（TG 已報，需人手撳 Start）
  4 = 連 status API 都讀唔到
"""
import json
import os
import re
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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def tg(msg):
    print(f"[TG] {msg}")
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
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
        hb = p[0].get("hb")
        if hb:
            return int((time.time() * 1000 - hb) / 1000)
        return int(p[0].get("hbAgo", 999999))
    except Exception as e:
        print("status err:", e)
        return None


def panel_session():
    """起一個過咗 Mitelis gate-1（mit_ck_p2）嘅 session。"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Accept-Language": "zh-CN,zh;q=0.9"})
    return s


def gate1_pass(s):
    """GET 一次，若係 mit_ck_p2 閘頁就抽 token 落 cookie。回 (ok, reason)。"""
    r = s.get(f"{PANEL}/api/client", timeout=25)
    ct = r.headers.get("content-type", "")
    if r.status_code == 403:
        return False, "IP 撞正 Mitelis 403 硬牆（未過 gate-1）"
    if "json" in ct:
        return True, None  # 冇閘，直接通
    m = re.search(r"\}\)\('([^']+)'\)", r.text)
    if m:
        s.cookies.set("mit_ck_p2", m.group(1), domain="rustix.me", path="/")
        return True, None
    return False, f"未知 HTML 回應 HTTP {r.status_code}（前 120 字：{r.text[:120]}）"


def api(s, path, method="GET", payload=None):
    """帶 key 打 panel API。回 (status, json_or_None, fail_reason)。"""
    h = {"Authorization": f"Bearer {PTERO_KEY}",
         "Accept": "application/json",
         "Content-Type": "application/json"}
    r = s.request(method, f"{PANEL}{path}", json=payload, headers=h, timeout=25)
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        try:
            return r.status_code, (r.json() if r.text else {}), None
        except Exception:
            return r.status_code, None, f"JSON 解析失敗 HTTP {r.status_code}"
    if "jsc-post" in r.text or "challengeTag" in r.text:
        return r.status_code, None, "撞正 jsc-post-v3 PoW 牆（gate-2，瀏覽器先過到）"
    return r.status_code, None, f"非 JSON 回應 HTTP {r.status_code}（前 120 字：{r.text[:120]}）"


def try_restart():
    """行成條重啟鏈。回 (ok, detail)。"""
    if not PTERO_KEY:
        return False, "冇 RUSTIX_PTERO_KEY secret"

    s = panel_session()
    ok, reason = gate1_pass(s)
    if not ok:
        return False, reason

    st, data, reason = api(s, "/api/client")
    if reason:
        return False, f"GET /api/client → {reason}"
    if st != 200:
        return False, f"GET /api/client → HTTP {st}: {json.dumps(data, ensure_ascii=False)[:150]}"
    servers = (data or {}).get("data", [])
    target = None
    for sv in servers:
        a = sv.get("attributes", {})
        if a.get("identifier", "").startswith(UUID_PREFIX) or UUID_PREFIX in a.get("uuid", ""):
            target = a
            break
    if not target:
        ids = [sv.get("attributes", {}).get("identifier") for sv in servers]
        return False, f"server list 冇 {UUID_PREFIX}（搵到 {ids}）"
    sid = target["identifier"]
    print(f"server: {sid} ({target.get('name')}) suspended={target.get('is_suspended')}")

    state = "unknown"
    st3, res, reason3 = api(s, f"/api/client/servers/{sid}/resources")
    if reason3:
        print(f"resources 讀唔到（{reason3}），照樣試 power")
    else:
        state = (res or {}).get("attributes", {}).get("current_state", "unknown")
        print(f"current_state: {state}")

    st4, body4, reason4 = api(s, f"/api/client/servers/{sid}/power",
                              method="POST", payload={"signal": "start"})
    if reason4:
        return False, f"POST power → {reason4}"
    if st4 in (200, 202, 204):
        return True, f"power start 已發出（state={state}, HTTP {st4}）"
    # panel 有 JSON 回應（鏈路通，但 panel 話唔得）
    det = json.dumps(body4, ensure_ascii=False)[:200] if body4 else ""
    already = "already" in det.lower()
    if already:
        return True, f"server 已經開緊（state={state}），POST 冇副作用；鏈路全通"
    return False, f"POST power → HTTP {st4}: {det}"


def main():
    hb = get_hb_age()
    if hb is None:
        tg("⚠️ Rustix watchdog：連 weather.2088x.com/__status 都讀唔到，睇下 Worker 係咪死咗")
        sys.exit(4)
    print(f"heartbeat age: {hb}s (limit {STALE_LIMIT}s)")

    if hb <= STALE_LIMIT and not TEST_RESTART:
        print("✅ RUSTIX_OK")
        sys.exit(0)

    mode = "TEST" if TEST_RESTART else "OFFLINE"
    print(f"[{mode}] 行自動重啟鏈路…")
    ok, detail = try_restart()
    print(f"restart chain: ok={ok} detail={detail}")

    if TEST_RESTART:
        if ok:
            tg(f"🧪 Rustix 自動重啟鏈路測試：✅ 全通\n{detail}\n下次熄機 GHA 會自己撳掣，唔使再等你")
            sys.exit(0)
        tg(f"🧪 Rustix 自動重啟鏈路測試：❌ 失敗\n{detail}\n繼續用「TG 叫人 → 人手撳 Start」後備")
        sys.exit(3)

    if ok:
        tg(f"🤖 Rustix offline（{hb}s 無心跳）→ 已自動重啟\n{detail}\n等 75 秒複查心跳…")
        time.sleep(75)
        hb2 = get_hb_age()
        if hb2 is not None and hb2 <= 120:
            tg(f"✅ Rustix 自動重啟成功，心跳已恢復（age={hb2}s）")
            sys.exit(2)
        tg(f"⚠️ power start 已發出但心跳未返（age={hb2}s），可能要再等多陣，30 分鐘後 watchdog 會再查")
        sys.exit(2)

    tg(f"🚨 Rustix offline（{hb}s 無心跳），自動重啟失敗：{detail}\n請人手去 panel 撳 Start")
    sys.exit(3)


if __name__ == "__main__":
    main()
