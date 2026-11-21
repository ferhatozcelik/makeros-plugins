#!/usr/bin/env python3
"""Eklentileri başlatır ve çalışıp çalışmadıklarını denetler (duman testi).

İki kip:

  Yerel (MakerOS gerekmez): eklentiyi geçici bir klasöre kopyalar, makerd'nin yaptığı gibi
  `uv run main.py` ile MAKEROS_PLUGIN_PORT/MAKEROS_PLUGIN_ID ortamında başlatır, sonra denetler.
      python3 tools/smoke.py                       # tüm eklentiler
      python3 tools/smoke.py pihole chromecast     # yalnız bunlar

  MakerOS (gerçek kurulum): zip'i makerd'ye içe aktarır, Eklentiler → Başlat'ı çağırır ve
  denetimleri makerd'nin /plugin/<id>/ vekili üzerinden yapar.
      python3 tools/smoke.py --makeros http://localhost --user maker --password maker

Denetimler (her eklenti):
  1. süreç ayakta kalıyor ve /health 200 {"ok": true} dönüyor (ilk başlatmada uv bağımlılıkları kurar)
  2. / 200 ve HTML dönüyor (arayüz)
  3. /health "state" bildiriyorsa (ör. model indiren eklenti) "ready" olana dek beklenir; "error" başarısızlıktır
  4. --set ile ayar verildiyse POST /api/config yapılır ve /health "connected": true olmalı

Ayar örneği (Pi-hole/Home Assistant'ı gerçekten bağlayarak dener):
      --set pihole='{"url":"http://192.168.1.2","password":"..."}'
      --set homeassistant='{"url":"http://ha.local:8123","token":"..."}'

Çıkış kodu: hepsi geçtiyse 0, değilse 1.
"""

import argparse
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from build import build_zip  # noqa: E402  (aynı paketleme kuralları)

GREEN, RED, DIM, END = ("\033[32m", "\033[31m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("", "", "", "")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Http:
    def __init__(self, base, headers=None):
        self.base = base.rstrip("/")
        self.headers = headers or {}
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def req(self, method, path, body=None, ctype="application/json", timeout=30):
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=dict(self.headers))
        if data is not None:
            r.add_header("Content-Type", ctype)
        try:
            with self.op.open(r, timeout=timeout) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def json(self, method, path, body=None, **kw):
        code, _, raw = self.req(method, path, body, **kw)
        try:
            return code, json.loads(raw or b"{}")
        except ValueError:
            return code, {"_raw": raw[:200].decode(errors="replace")}


class Result:
    def __init__(self, pid):
        self.id, self.steps, self.ok = pid, [], True

    def step(self, ok, msg):
        self.steps.append((ok, msg))
        self.ok &= ok
        print(f"  {GREEN + '✓' if ok else RED + '✗'}{END} {msg}", flush=True)
        return ok


def wait_health(get, alive, timeout):
    """/health 200 ok:true olana dek bekler. (durum, sözlük|None, hata metni)"""
    t0, last = time.time(), "yanıt yok"
    while time.time() - t0 < timeout:
        if not alive():
            return False, None, "süreç sonlandı"
        try:
            code, d = get("/health")
            if code == 200 and d.get("ok"):
                return True, d, ""
            if code == 404 or (code == 200 and "_raw" in d):
                return True, {"_nohealth": True}, ""  # /health yok (eski eklenti): sunucunun yanıt vermesi yeterli
            last = f"HTTP {code}: {d}"
        except (OSError, urllib.error.URLError) as e:
            last = str(e)
        time.sleep(1)
    return False, None, f"{timeout} sn içinde hazır olmadı ({last})"


def checks(r, get, req, alive, cfg, timeout):
    t0 = time.time()
    ok, h, err = wait_health(get, alive, timeout)
    took = time.time() - t0
    if h and h.pop("_nohealth", False):
        r.step(True, f"sunucu yanıt veriyor ({took:.1f} sn; /health uç noktası yok)")
    elif not r.step(ok, f"/health ok ({took:.1f} sn)" if ok else f"/health: {err}"):
        return
    code, ctype, body = req("GET", "/")
    r.step(code == 200 and "text/html" in ctype and b"<" in body[:200], f"arayüz / → {code} {ctype.split(';')[0]}")
    if "state" in h:
        t1 = time.time()
        while h.get("state") not in ("ready", "error") and time.time() - t1 < timeout and alive():
            time.sleep(1)
            _, h = get("/health")
        extra = f", öz denetim {h['selftest_ms']} ms" if "selftest_ms" in h else ""
        r.step(h.get("state") == "ready", f"durum: {h.get('state')}{extra}" + (f" — {h['error']}" if h.get("error") else ""))
    if cfg is not None:
        code, ctype, body = req("POST", "/api/config", json.dumps(cfg).encode())
        r.step(code == 200, f"ayar kaydedildi ve bağlantı denendi → {code} {body[:160].decode(errors='replace') if code != 200 else ''}".rstrip())
        _, h = get("/health")
        r.step(bool(h.get("connected")), "bağlı: " + ("evet" + (f" (sürüm {h['version']})" if h.get("version") else "") if h.get("connected") else f"hayır — {h.get('error', '?')}"))
    elif "configured" in h:
        print(f"  {DIM}· ayarlanmamış (bağlantı denemek için --set {r.id}='{{...}}'){END}")
    elif h:
        info = {k: v for k, v in h.items() if k not in ("ok", "plugin", "models")}
        print(f"  {DIM}· {json.dumps(info, ensure_ascii=False)[:200]}{END}")


def run_local(pid, d, cfg, timeout, keep):
    r = Result(pid)
    m = json.loads((d / "plugin.json").read_text(encoding="utf-8"))
    work = Path(tempfile.mkdtemp(prefix=f"mk-smoke-{pid}-"))
    shutil.copytree(d, work / pid, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".log", ".pid"))
    port = free_port()
    env = {**os.environ, "MAKEROS_PLUGIN_ID": pid, "MAKEROS_PLUGIN_PORT": str(port), "XDG_CONFIG_HOME": str(work / "config")}
    log = open(work / "log.txt", "w+")
    proc = subprocess.Popen(["uv", "run", "--quiet", m.get("entrypoint") or "main.py", *m.get("args", [])],
                            cwd=work / pid, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    h = Http(f"http://127.0.0.1:{port}")
    try:
        r.step(True, f"başlatıldı: uv run (port {port}, pid {proc.pid})")
        checks(r, lambda p: h.json("GET", p, timeout=5), lambda meth, p, b=None: h.req(meth, p, b, timeout=60), lambda: proc.poll() is None, cfg, timeout)
        r.step(proc.poll() is None, "süreç hâlâ çalışıyor")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
            r.step(True, "SIGTERM ile temiz kapandı")
        except subprocess.TimeoutExpired:
            proc.kill()
            r.step(False, "SIGTERM'e yanıt vermedi (öldürüldü)")
        if not r.ok:
            log.seek(0)
            print(DIM + "  --- günlük (son 30 satır) ---\n  " + "\n  ".join(log.read().splitlines()[-30:]) + END)
        log.close()
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    return r


def run_makeros(pid, d, cfg, timeout, mk):
    r = Result(pid)
    code, res = mk.json("POST", "/api/v1/plugins/import", build_zip(d), ctype="application/zip", timeout=60)
    if not r.step(code == 200 and res.get("id") == pid, f"MakerOS'a içe aktarıldı → {code}" + ("" if code == 200 else f" {res}")):
        return r
    code, res = mk.json("POST", f"/api/v1/plugins/{pid}/start", timeout=120)
    if not r.step(code == 200, f"Eklentiler → Başlat → {code}, çalışıyor={res.get('running')} pid={res.get('pid')}"):
        return r

    def alive():
        c, p = mk.json("GET", "/api/v1/plugins")
        return c == 200 and any(x.get("id") == pid and x.get("running") for x in p)

    base = f"/plugin/{pid}"
    checks(r, lambda p: mk.json("GET", base + p, timeout=10), lambda meth, p, b=None: mk.req(meth, base + p, b, timeout=60), alive, cfg, timeout)
    r.step(alive(), "makerd eklentiyi çalışıyor görüyor")
    if not r.ok:
        _, lg = mk.json("POST", f"/api/v1/plugins/{pid}/logs")
        print(DIM + "  --- eklenti günlüğü (son 30 satır) ---\n  " + "\n  ".join((lg.get("log") or "").splitlines()[-30:]) + END)
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="*", help="eklenti kimlikleri (boşsa hepsi)")
    ap.add_argument("--makeros", metavar="URL", help="çalışan MakerOS adresi (ör. http://localhost); verilirse içe aktarıp orada başlatır")
    ap.add_argument("--user", default="maker")
    ap.add_argument("--password", default="maker")
    ap.add_argument("--set", action="append", default=[], metavar="ID=JSON", help="eklentiye POST /api/config ile verilecek ayar")
    ap.add_argument("--timeout", type=int, default=300, help="her adım için en uzun bekleme (sn; ilk uv kurulumu uzun sürebilir)")
    ap.add_argument("--keep", action="store_true", help="yerel kipte geçici klasörü silme")
    ap.add_argument("--stop", action="store_true", help="MakerOS kipinde testten sonra eklentiyi durdur")
    a = ap.parse_args()

    cfgs = {}
    for s in a.set:
        k, _, v = s.partition("=")
        try:
            cfgs[k] = json.loads(v)
        except ValueError:
            sys.exit(f"--set {k}: geçerli JSON değil")
    dirs = {p.parent.name: p.parent for p in sorted((ROOT / "plugins").glob("*/plugin.json"))}
    ids = a.ids or list(dirs)
    for i in ids:
        if i not in dirs:
            sys.exit(f"eklenti yok: {i} (var olanlar: {', '.join(dirs)})")
    if not a.makeros and not shutil.which("uv"):
        sys.exit("uv bulunamadı: https://docs.astral.sh/uv/ (MakerOS'ta hazır gelir)")

    mk = None
    if a.makeros:
        mk = Http(a.makeros, {"X-Maker": "1"})
        code, res = mk.json("POST", "/api/v1/auth/login", {"user": a.user, "password": a.password})
        if code != 200:
            sys.exit(f"MakerOS girişi başarısız ({code}): {res}")
        print(f"MakerOS: {a.makeros} ({a.user} olarak giriş yapıldı)")

    results = []
    for pid in ids:
        print(f"\n▶ {pid}")
        r = run_makeros(pid, dirs[pid], cfgs.get(pid), a.timeout, mk) if mk else run_local(pid, dirs[pid], cfgs.get(pid), a.timeout, a.keep)
        if mk and a.stop:
            mk.json("POST", f"/api/v1/plugins/{pid}/stop")
        results.append(r)

    print("\nÖzet")
    for r in results:
        print(f"  {GREEN + 'GEÇTİ ' if r.ok else RED + 'KALDI '}{END} {r.id}")
    sys.exit(0 if all(r.ok for r in results) else 1)


if __name__ == "__main__":
    main()
