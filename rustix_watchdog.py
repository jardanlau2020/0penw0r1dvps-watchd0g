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


JS_FETCH_RAW = """
const [path, method, body, key, cb] = arguments;
const h = {'Accept': 'application/json'};
if (key) { h['Authorization'] = 'Bearer ' + key; }
if (method !== 'GET') { h['Content-Type'] = 'application/json'; }
const ctl = new AbortController();
setTimeout(function(){ ctl.abort(); }, 20000);
fetch(path, {
  method: method,
  credentials: 'include',
  headers: h,
  body: (method === 'GET') ? undefined : body,
  signal: ctl.signal
}).then(function(r) {
  return r.text().then(function(t) {
    cb(JSON.stringify({status: r.status, body: t.slice(0, 6000)}));
  });
}).catch(function(e) {
  cb(JSON.stringify({status: 0, body: String(e).slice(0, 300)}));
});
"""


def js_call(sb, path, method="GET", payload=None, key=None):
    """瀏覽器內 fetch（乾淨版：冇 fallback，如實回報）。"""
    body = json.dumps(payload) if (method != "GET" and payload is not None) else None
    try:
        raw = sb.driver.execute_async_script(JS_FETCH_RAW, path, method, body, key)
        d = json.loads(raw)
        return d.get("status", 0), d.get("body", ""), None
    except Exception as e:
        return 0, "", "execute_async_script 拋例外：" + repr(e)[:150]


def try_restart():
    """診斷電池 v3：runner 直連指紋對照 + 瀏覽器頁面法證 + PoW cookie 時間線。

    Part A：runner 直連（requests + curl_cffi 五檔 TLS 指紋逐檔試）
      → 邊檔過到牆？過到嘅即刻帶 key 打 /api/client，通就直頭行重啟鏈
    Part B：瀏覽器開主頁後頁面法證 dump
      （location.href 睇係咪 chrome-error 頁、window.fetch 係咪被 override、
        ServiceWorker、CSP、script 清單、navigation timing）
      + fetch 對照組（/favicon.ico vs / vs /api/client）
      + 60 秒時間線睇 PoW 會唔會遲啲先 set cookie
      + 最後 driver.get /api/client 攞 ERR code
    """
    import re as _re

    # ---------- Part A：runner 直連指紋對照 ----------
    try:
        r = requests.get(PANEL + "/", timeout=20, headers={
            "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        print("[A1 requests /] st=%s len=%d ct=%s set-cookie=%s" % (
            r.status_code, len(r.text), r.headers.get("content-type", "")[:40],
            r.headers.get("set-cookie", "")[:100]), flush=True)
    except Exception as e:
        print("[A1 requests /] exception: %s" % repr(e)[:150], flush=True)

    try:
        from curl_cffi import requests as cffi
    except Exception:
        cffi = None
        print("[A2] curl_cffi import 失敗", flush=True)

    best = None
    if cffi:
        for imp in ("chrome131", "chrome124", "chrome120", "chrome116", "chrome110"):
            try:
                r = cffi.get(PANEL + "/", impersonate=imp, timeout=20)
                print("[A2 cffi %s /] st=%s len=%d set-cookie=%s" % (
                    imp, r.status_code, len(r.text),
                    r.headers.get("set-cookie", "")[:100]), flush=True)
                if r.status_code != 200:
                    continue
                r2 = cffi.get(PANEL + "/api/client", impersonate=imp, timeout=20, headers={
                    "Authorization": "Bearer " + PTERO_KEY,
                    "Accept": "application/json"})
                print("[A2 cffi %s api] st=%s body[:150]=%r" % (
                    imp, r2.status_code, r2.text[:150]), flush=True)
                if r2.status_code == 200 and '"data"' in r2.text:
                    best = (imp, r2.text)
                    break
            except Exception as e:
                print("[A2 cffi %s] exception: %s" % (imp, repr(e)[:120]), flush=True)

    if best:
        imp, body = best
        print("[A] 搵到通嘅指紋檔：%s — 即刻行重啟鏈" % imp, flush=True)
        data = parse_json(body)
        servers = (data or {}).get("data") or []
        target = None
        for sv in servers:
            a = sv.get("attributes", {})
            if a.get("identifier", "").startswith(UUID_PREFIX) or UUID_PREFIX in str(a.get("uuid", "")):
                target = a
                break
        if not target:
            return False, "curl_cffi(%s) 過咗牆但 server list 冇 %s（共 %d 台：%s）" % (
                imp, UUID_PREFIX, len(servers),
                str([sv.get("attributes", {}).get("identifier") for sv in servers][:8]))
        sid = target["identifier"]
        print("server: %s (%s)" % (sid, target.get("name")), flush=True)
        rr = cffi.post(PANEL + "/api/client/servers/" + sid + "/power",
                       impersonate=imp, timeout=20, json={"signal": "start"},
                       headers={"Authorization": "Bearer " + PTERO_KEY,
                                "Accept": "application/json"})
        print("[A power] st=%s body[:150]=%r" % (rr.status_code, rr.text[:150]), flush=True)
        if rr.status_code in (200, 201, 202, 204):
            return True, "curl_cffi(%s) 全鏈通：power start 已發出（HTTP %s）" % (imp, rr.status_code)
        low = rr.text.lower()
        if "already" in low or "running" in low:
            return True, "curl_cffi(%s) 鏈路全通（server 已開緊，POST 冇副作用）" % imp
        return False, "curl_cffi(%s) POST power HTTP %s: %s" % (imp, rr.status_code, rr.text[:150])

    # ---------- Part B：瀏覽器頁面法證 ----------
    from seleniumbase import SB

    print("[gate] SB starting (uc=True, headless=False)...", flush=True)
    with SB(uc=True, headless=False) as sb:
        try:
            sb.driver.set_script_timeout(45)
            sb.driver.set_page_load_timeout(45)
        except Exception:
            pass
        print("[gate] uc_open_with_reconnect...", flush=True)
        sb.uc_open_with_reconnect(PANEL, reconnect_time=6)
        sb.sleep(5)

        DUMP_JS = """
            var r = {};
            r.href = location.href; r.title = document.title; r.rs = document.readyState;
            r.cookie = document.cookie;
            r.fetchNative = String(window.fetch).indexOf('native code') >= 0;
            r.fetchSrc = String(window.fetch).slice(0, 100);
            try { r.sw = (navigator.serviceWorker && navigator.serviceWorker.controller) ? String(navigator.serviceWorker.controller.scriptURL) : 'none'; } catch(e) { r.sw = 'err'; }
            try { r.ls = Object.keys(localStorage).join(','); } catch(e) { r.ls = 'err'; }
            try { r.ss = Object.keys(sessionStorage).join(','); } catch(e) { r.ss = 'err'; }
            try {
              var cs = [];
              document.querySelectorAll('meta[http-equiv]').forEach(function(m) {
                cs.push(m.getAttribute('http-equiv') + '=' + (m.getAttribute('content')||'').slice(0, 120));
              });
              r.csp = cs.join(' | ') || 'none';
            } catch(e) { r.csp = 'err'; }
            var h = [];
            document.querySelectorAll('h1,h2').forEach(function(e) { if (h.length < 5 && e.textContent.trim()) h.push(e.textContent.trim()); });
            r.heads = h.join(' / ') || 'none';
            var fs = [];
            document.querySelectorAll('form').forEach(function(f) { if (fs.length < 3) fs.push((f.method||'?') + '->' + (f.action||'?')); });
            r.forms = fs.join(' | ') || 'none';
            var sc = [];
            document.querySelectorAll('script[src]').forEach(function(s) { if (sc.length < 8) sc.push(s.src); });
            r.scripts = sc.join(' ; ');
            r.text = document.body ? document.body.innerText.slice(0, 250) : '';
            try {
              var n = performance.getEntriesByType('navigation')[0];
              if (n) { r.nav = (n.responseStatus||'?') + '|' + (n.redirectCount||0) + '|' + (n.nextHopProtocol||'?') + '|' + Math.round(n.responseStart||0) + 'ms'; }
            } catch(e) { r.nav = 'err'; }
            return r;
        """

        def dump(tag):
            try:
                info = sb.driver.execute_script(DUMP_JS) or {}
                for k in ("href", "title", "rs", "nav", "fetchNative", "fetchSrc",
                          "sw", "ls", "ss", "csp", "heads", "forms"):
                    print("[%s] %s: %s" % (tag, k, str(info.get(k))[:200]), flush=True)
                print("[%s] cookie: %r" % (tag, str(info.get("cookie"))[:200]), flush=True)
                print("[%s] text[:250]: %r" % (tag, str(info.get("text"))[:250]), flush=True)
                for s in str(info.get("scripts", "")).split(" ; ")[:8]:
                    if s and s != "None":
                        print("[%s] script: %s" % (tag, s[:150]), flush=True)
                return info
            except Exception as e:
                print("[%s] dump err: %s" % (tag, repr(e)[:150]), flush=True)
                return {}

        dump("P0")

        for p in ("/favicon.ico", "/", "/api/client"):
            st, body, rsn = js_call(sb, p)
            print("[B-fetch %s] st=%s body[:120]=%r rsn=%s" % (p, st, (body or "")[:120], rsn), flush=True)

        # PoW cookie 時間線：60 秒內每 6 秒睇一次
        for i in range(10):
            sb.sleep(6)
            try:
                ck = sb.driver.get_cookies()
                tt = sb.get_title()
                st, _b, _r = js_call(sb, "/favicon.ico")
                print("[T%02d] t+%ds cookies=%s title=%r fav-st=%s" % (
                    i, (i + 1) * 6, [c["name"] for c in ck], (tt or "")[:30], st), flush=True)
                if ck:
                    break
            except Exception as e:
                print("[T%02d] err %s" % (i, repr(e)[:100]), flush=True)

        dump("P1")

        st5, b5, r5 = js_call(sb, "/api/client", key=PTERO_KEY)
        print("[B-key api] st=%s body[:200]=%r rsn=%s" % (st5, (b5 or "")[:200], r5), flush=True)
        if st5 == 200 and '"data"' in (b5 or ""):
            data = parse_json(b5)
            servers = (data or {}).get("data") or []
            target = None
            for sv in servers:
                a = sv.get("attributes", {})
                if a.get("identifier", "").startswith(UUID_PREFIX) or UUID_PREFIX in str(a.get("uuid", "")):
                    target = a
                    break
            if not target:
                return False, "瀏覽器 fetch 過咗但 server list 冇 " + UUID_PREFIX
            sid = target["identifier"]
            print("server: %s (%s)" % (sid, target.get("name")), flush=True)
            st4, b4, r4 = js_call(sb, "/api/client/servers/" + sid + "/power",
                                  method="POST", payload={"signal": "start"}, key=PTERO_KEY)
            print("[B power] st=%s body[:150]=%r rsn=%s" % (st4, (b4 or "")[:150], r4), flush=True)
            if st4 in (200, 201, 202, 204):
                return True, "瀏覽器 fetch 全鏈通：power start 已發出（HTTP %s）" % st4
            low = (b4 or "").lower()
            if "already" in low or "running" in low:
                return True, "瀏覽器鏈路全通（server 已開緊，POST 冇副作用）"
            return False, "POST power HTTP %s: %s" % (st4, (b4 or "")[:150])

        try:
            sb.driver.get(PANEL + "/api/client")
            sb.sleep(2)
            src = sb.get_page_source() or ""
            m = _re.search(r"ERR_[A-Z0-9_]+", src)
            print("[B-nav api] err=%s title=%r len=%d" % (
                m.group(0) if m else None, (sb.get_title() or "")[:40], len(src)), flush=True)
        except Exception as e:
            print("[B-nav api] exception: %s" % repr(e)[:150], flush=True)

        try:
            cks = [c["name"] for c in sb.driver.get_cookies()]
        except Exception:
            cks = []
        return False, "診斷 v3 完：瀏覽器 route 全斷（cookies=%s），睇 Part A 對照結果" % cks


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
