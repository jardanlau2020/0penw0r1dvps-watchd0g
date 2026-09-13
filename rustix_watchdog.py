#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rustix watchdog v5：心跳檢查 + 真瀏覽器過 Mitelis 牆 + 瀏覽器內 fetch 重啟。

流程：
  1. 讀 weather.2088x.com/__status 嘅 rustix 心跳（唔經牆）
  2. 心跳健康 → exit 0（唔掂 panel，零風險）
  3. offline 或 TEST_RESTART → xvfb-run 內起 headed chromium（uc mode）
     → uc_open_with_reconnect 過 Mitelis 閘（mit_ck_p2 + jsc-post-v3 PoW）
     → 喺瀏覽器內用 JS fetch 打 panel API（同 TLS 指紋，免 TLSV1_ALERT_INTERNAL_ERROR）
     → 搵 server → POST power start → 75s 後複查心跳 → TG 報結果

教訓記錄（runs 34774635660~34777115243 驗屍）：
  - SB(xvfb=True) 同外層 xvfb-run 打交 → 零 cookies
  - sb.open() 撞 UC 斷線反偵測 → chromedriver Connection refused
    （要用 uc_open_with_reconnect，bot-hosting 同解）
  - 瀏覽器過閘後攞 cookie 轉手 requests → Mitelis TLS 指紋牆
    TLSV1_ALERT_INTERNAL_ERROR → 一切 API 呼叫都要留喺瀏覽器內 fetch

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


JS_FETCH = """
const [path, method, body, key, cb] = arguments;
fetch(path, {
  method: method,
  credentials: 'include',
  headers: {
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + key
  },
  body: (method === 'GET') ? undefined : body
}).then(function(r) {
  return r.text().then(function(t) {
    cb(JSON.stringify({status: r.status, body: t.slice(0, 6000)}));
  });
}).catch(function(e) {
  cb(JSON.stringify({status: 0, body: String(e).slice(0, 500)}));
});
"""


def js_fetch(sb, path, method="GET", payload=None):
    """瀏覽器內 fetch panel API。回 (status, body_text, fail_reason)。"""
    body = None
    if method != "GET" and payload is not None:
        body = json.dumps(payload)
    try:
        raw = sb.driver.execute_async_script(
            JS_FETCH, path, method, body, PTERO_KEY)
    except Exception as e:
        print("async script err:", repr(e)[:150], flush=True)
        raw = None
    if raw:
        try:
            d = json.loads(raw)
            if d.get("status", 0) > 0:
                return d.get("status", 0), d.get("body", ""), None
            if "Failed to fetch" not in str(d.get("body", "")):
                return d.get("status", 0), d.get("body", ""), None
        except Exception:
            pass
    # fallback：driver.get 直接開 API URL（同源 navigation，帶齊 cookies）— GET only
    if method == "GET":
        print("[fetch-fallback] driver.get " + path, flush=True)
        try:
            sb.driver.get(PANEL + path)
            sb.sleep(1)
            text = sb.driver.find_element("tag name", "pre").text \
                if sb.driver.find_elements("tag name", "pre") else (sb.get_page_source() or "")
            import re as _re
            m = _re.search(r"\{.*\}", text, _re.S)
            if m:
                return 200, m.group(0), None
            return 200, text[:3000], None
        except Exception as e:
            return 0, "", "driver.get fallback 失敗：" + repr(e)[:150]
    return 0, "", "fetch TypeError（CORS/被斷）＋非 GET 冇 fallback" 
    try:
        d = json.loads(raw)
        return d.get("status", 0), d.get("body", ""), None
    except Exception:
        return 0, str(raw)[:300], "JS 回應解析失敗"


JS_XHR = """
const [path, key, cb] = arguments;
try {
  const xhr = new XMLHttpRequest();
  xhr.open('GET', path, true);
  xhr.setRequestHeader('Accept', 'application/json');
  xhr.setRequestHeader('Authorization', 'Bearer ' + key);
  xhr.onload = function() { cb(JSON.stringify({status: xhr.status, body: xhr.responseText.slice(0, 6000)})); };
  xhr.onerror = function() { cb(JSON.stringify({status: 0, body: 'XHR network error'})); };
  xhr.send();
} catch (e) { cb(JSON.stringify({status: 0, body: String(e).slice(0, 300)})); }
"""


def js_xhr(sb, path):
    """瀏覽器內 XHR GET。回 (status, body, fail_reason)。"""
    try:
        raw = sb.driver.execute_async_script(JS_XHR, path, PTERO_KEY)
        d = json.loads(raw)
        return d.get("status", 0), d.get("body", ""), None
    except Exception as e:
        return 0, "", "XHR 失敗：" + repr(e)[:150]


def parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def wait_gate_pass(sb):
    """等 Mitelis 閘過（等 panel 頁面特徵出現）。回 fail_reason or None。"""
    deadline = time.time() + 90
    clicked = False
    while time.time() < deadline:
        try:
            title = sb.get_title()
            src = sb.get_page_source() or ""
        except Exception as e:
            print("poll err:", repr(e)[:120])
            title, src = "", ""
        gated = ("challengeTag" in src) or ("mit_ck" in src) or ("FsGtA7wj" in src)
        print("wait: title=%r len=%d gated=%s" % (title[:40], len(src), gated), flush=True)
        if ("Pterodactyl" in title or "pterodactyl" in src.lower()
                or "Sign in to continue" in src):
            sb.sleep(2)
            return None
        if not clicked and gated:
            try:
                sb.uc_gui_click_captcha()
                clicked = True
                sb.sleep(2)
                print("uc_gui_click_captcha done", flush=True)
            except Exception as e:
                print("captcha click err:", repr(e)[:120])
        sb.sleep(3)
    try:
        sb.save_screenshot("rustix_gate.png")
    except Exception:
        pass
    try:
        with open("rustix_gate.html", "w", encoding="utf-8") as f:
            f.write(sb.get_page_source() or "")
    except Exception:
        pass
    return "90s 內未見到 panel 頁面特徵（閘未過或頁面唔同預期）"


def try_restart():
    """行成條重啟鏈（瀏覽器內）。回 (ok, detail)。"""
    if not PTERO_KEY:
        return False, "冇 RUSTIX_PTERO_KEY secret"

    from seleniumbase import SB

    print("[gate] SB starting (uc=True, headless=False)...", flush=True)
    with SB(uc=True, headless=False) as sb:
        try:
            sb.driver.set_script_timeout(40)
        except Exception:
            pass
        print("[gate] SB started, uc_open_with_reconnect...", flush=True)
        sb.uc_open_with_reconnect(PANEL, reconnect_time=6)
        sb.sleep(3)
        reason = wait_gate_pass(sb)
        if reason:
            return False, "過閘失敗：" + reason
        print("[gate] passed", flush=True)

        # ==== 診斷電池：試勻各種方法，揀到 200+JSON 嘅就停 ====
        methods = {}
        # m1: fetch 相對路徑
        st, body, reason = js_fetch(sb, "/api/client")
        print("[diag m1 fetch-rel] st=%s reason=%s body[:120]=%s" % (st, reason, body[:120]), flush=True)
        methods["fetch-rel"] = (st, body, reason)
        # m2: fetch 絕對 URL
        if not (st == 200 and '"data"' in body):
            st, body, reason = js_fetch(sb, PANEL + "/api/client")
            print("[diag m2 fetch-abs] st=%s reason=%s body[:120]=%s" % (st, reason, body[:120]), flush=True)
            methods["fetch-abs"] = (st, body, reason)
        # m3: XHR
        if not (st == 200 and '"data"' in body):
            st, body, reason = js_xhr(sb, "/api/client")
            print("[diag m3 xhr] st=%s reason=%s body[:120]=%s" % (st, reason, body[:120]), flush=True)
            methods["xhr"] = (st, body, reason)
        # m4: curl_cffi 帶瀏覽器 cookies + Chrome TLS 指紋
        if not (st == 200 and '"data"' in body):
            try:
                from curl_cffi import requests as cffi
                ck = {c["name"]: c["value"] for c in sb.driver.get_cookies()}
                ua = sb.driver.execute_script("return navigator.userAgent")
                r = cffi.get(PANEL + "/api/client",
                             headers={"Authorization": "Bearer " + PTERO_KEY,
                                      "Accept": "application/json",
                                      "User-Agent": ua},
                             cookies=ck, impersonate="chrome", timeout=25)
                st, body, reason = r.status_code, r.text, None
                print("[diag m4 curlcffi] st=%s body[:120]=%s" % (st, body[:120]), flush=True)
                methods["curlcffi"] = (st, body, reason)
            except Exception as e:
                print("[diag m4 curlcffi] exception:", repr(e)[:150], flush=True)
                methods["curlcffi"] = (0, "", repr(e)[:150])
        if not (st == 200 and '"data"' in body):
            det = "; ".join("%s→HTTP%s" % (k, v[0]) for k, v in methods.items())
            return False, "GET /api/client 全部方法失敗：" + det
        print("[api] 用到嘅方法 body[:200]: %s" % body[:200], flush=True)
        data = parse_json(body)
        servers = (data or {}).get("data") or []
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
        print("server: " + sid + " (" + str(target.get("name")) + ") suspended=" + str(target.get("is_suspended")), flush=True)

        st3, body3, reason3 = js_fetch(sb, "/api/client/servers/" + sid + "/resources")
        state = "unknown"
        if reason3:
            print("resources 讀唔到（" + reason3 + "），照樣試 power")
        else:
            res = parse_json(body3)
            state = ((res or {}).get("attributes") or {}).get("current_state", "unknown")
            print("current_state: " + str(state), flush=True)

        st4, body4, reason4 = js_fetch(sb, "/api/client/servers/" + sid + "/power",
                                       method="POST", payload={"signal": "start"})
        if reason4 or st4 == 0:
            # curl_cffi POST fallback（POST 冇 driver.get 後備）
            try:
                from curl_cffi import requests as cffi
                ck = {c["name"]: c["value"] for c in sb.driver.get_cookies()}
                ua = sb.driver.execute_script("return navigator.userAgent")
                r = cffi.post(PANEL + "/api/client/servers/" + sid + "/power",
                              headers={"Authorization": "Bearer " + PTERO_KEY,
                                       "Accept": "application/json",
                                       "Content-Type": "application/json",
                                       "User-Agent": ua},
                              cookies=ck, json={"signal": "start"},
                              impersonate="chrome", timeout=25)
                st4, body4, reason4 = r.status_code, r.text, None
                print("[api] POST power (curlcffi) HTTP %s body[:150]: %s" % (st4, body4[:150]), flush=True)
            except Exception as e:
                print("[api] POST power curlcffi exception:", repr(e)[:150], flush=True)
        if reason4 and st4 == 0:
            # XHR POST 後備
            st4, body4, reason4 = js_fetch(sb, "/api/client/servers/" + sid + "/power",
                                           method="POST", payload={"signal": "start"})
        if reason4:
            return False, "POST power → " + reason4
        print("[api] POST power HTTP %s body[:200]: %s" % (st4, body4[:200]), flush=True)
        if st4 in (200, 201, 202, 204):
            return True, "power start 已發出（state=" + str(state) + ", HTTP " + str(st4) + "）"
        low = body4.lower()
        if "already" in low or "running" in low:
            return True, "server 已經開緊（state=" + str(state) + "），POST 冇副作用；鏈路全通"
        return False, "POST power → HTTP " + str(st4) + ": " + body4[:200]


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
    try:
        ok, detail = try_restart()
    except Exception as e:
        import traceback
        traceback.print_exc()
        ok, detail = False, "鏈路 exception：" + repr(e)[:200]
    print("restart chain: ok=" + str(ok) + " detail=" + detail, flush=True)

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
