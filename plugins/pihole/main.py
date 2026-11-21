"""Pi-hole eklentisi: ağdaki bir Pi-hole'un (v6 API) istatistikleri ve reklam engelleme anahtarı.

Pi-hole'u kurmaz; var olan bir Pi-hole'a bağlanır. Bağımlılık yok (standart kitaplık).
Ayarlar (adres, parola) eklenti klasörü dışında, ~/.config/makeros-plugins/pihole.json (0600) dosyasında durur:
eklentiyi dışa aktarmak parolayı paylaşmaz, güncellemek ayarları silmez.

  GET  /                → arayüz
  GET  /health          → {"ok": true, "configured", "connected", "error"?}
  GET  /api/config      → {"url", "has_password", "insecure"}
  POST /api/config      ← {"url", "password"?, "insecure"?}  (bağlantıyı dener)
  GET  /api/summary     → engelleme durumu, sorgu/engellenen sayıları, en çok engellenen alan adları
  POST /api/blocking    ← {"enabled": bool, "timer": saniye|null}
"""
import errno
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLUGIN_ID = os.environ.get("MAKEROS_PLUGIN_ID", "pihole")
PORT = int(os.environ.get("MAKEROS_PLUGIN_PORT", "8791"))
CONF = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "makeros-plugins" / f"{PLUGIN_ID}.json"
UA = "MakerOS-pihole-eklentisi/1.0"


class PiholeError(Exception):
    pass


_req = threading.local()  # istek başına dil (ThreadingHTTPServer: istek = iş parçacığı)


def m(tr, en):
    """Hata/durum metni MakerOS dilinde (sayfa X-MakerOS-Lang başlığını gönderir)."""
    return en if getattr(_req, "lang", "tr") == "en" else tr


def load_conf():
    try:
        c = json.loads(CONF.read_text(encoding="utf-8"))
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def save_conf(c):
    CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(c, f)
    os.replace(tmp, CONF)


def norm_url(u):
    u = (u or "").strip().rstrip("/")
    if u and "://" not in u:
        u = "http://" + u
    if u.endswith("/admin"):
        u = u[: -len("/admin")]
    if u.endswith("/api"):
        u = u[: -len("/api")]
    return u


class Client:
    """Pi-hole v6 REST istemcisi (oturum kimliği SID, süresi dolunca yeniden giriş)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sid = None
        self.key = None  # (url, password) değişince oturum sıfırlanır

    def _ctx(self, c):
        if c.get("insecure"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    def _raw(self, c, method, path, body=None, sid=None):
        url = c["url"] + "/api" + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, "Accept": "application/json"})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if sid:
            req.add_header("X-FTL-SID", sid)
        try:
            with urllib.request.urlopen(req, timeout=8, context=self._ctx(c)) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            raise PiholeError(m(f"Pi-hole'a ulaşılamadı ({reason}).", f"Cannot reach Pi-hole ({reason}).")) from None
        except (TimeoutError, OSError) as e:
            raise PiholeError(m(f"Pi-hole'a ulaşılamadı ({e.__class__.__name__}).", f"Cannot reach Pi-hole ({e.__class__.__name__}).")) from None
        except ValueError:
            raise PiholeError(m("Yanıt JSON değil: adres bir Pi-hole v6 arayüzü mü?", "Response is not JSON: is this a Pi-hole v6 address?")) from None

    def _login(self, c):
        code, d = self._raw(c, "POST", "/auth", {"password": c.get("password", "")})
        s = d.get("session") or {}
        if code == 200 and s.get("valid"):
            return s.get("sid")  # parolasız Pi-hole'da None olabilir
        if code in (401, 400) or (code == 200 and not s.get("valid")):
            raise PiholeError(m("Parola yanlış (Pi-hole → Ayarlar → Web arayüzü/API parolası).", "Wrong password (Pi-hole → Settings → Web interface/API password)."))
        if code == 404:
            raise PiholeError(m("Bu adreste Pi-hole v6 API'si yok (v5 desteklenmez).", "No Pi-hole v6 API at this address (v5 is not supported)."))
        if code == 429:
            raise PiholeError(m("Pi-hole çok fazla oturum açıldığını bildiriyor; biraz bekleyin.", "Pi-hole reports too many sessions; wait a moment."))
        raise PiholeError(m(f"Giriş başarısız (HTTP {code}).", f"Login failed (HTTP {code})."))

    def call(self, method, path, body=None):
        c = load_conf()
        c["url"] = norm_url(c.get("url"))
        if not c["url"]:
            raise PiholeError(m("Önce Pi-hole adresini girin.", "Enter the Pi-hole address first."))
        with self.lock:
            key = (c["url"], c.get("password", ""), bool(c.get("insecure")))
            if key != self.key:
                self.sid, self.key = None, key
            for attempt in (0, 1):
                if self.sid is None:
                    self.sid = self._login(c) or ""
                code, d = self._raw(c, method, path, body, self.sid or None)
                if code == 401 and attempt == 0:
                    self.sid = None  # oturum süresi doldu
                    continue
                if code >= 400:
                    msg = (d.get("error") or {}).get("message") if isinstance(d, dict) else None
                    raise PiholeError(msg or m(f"Pi-hole HTTP {code} döndü.", f"Pi-hole returned HTTP {code}."))
                return d
        raise PiholeError(m("Oturum açılamadı.", "Could not sign in."))

    def logout(self):
        with self.lock:
            sid, self.sid = self.sid, None
            if sid:
                c = load_conf()
                c["url"] = norm_url(c.get("url"))
                try:
                    self._raw(c, "DELETE", "/auth", sid=sid)
                except PiholeError:
                    pass


PI = Client()


def summary():
    s = PI.call("GET", "/stats/summary")
    b = PI.call("GET", "/dns/blocking")
    try:
        top = PI.call("GET", "/stats/top_domains?blocked=true&count=8").get("domains", [])
    except PiholeError:
        top = []
    q = s.get("queries") or {}
    return {
        "ok": True,
        "blocking": b.get("blocking"),  # enabled | disabled | failed | unknown
        "timer": b.get("timer"),
        "queries": q.get("total", 0),
        "blocked": q.get("blocked", 0),
        "percent": round(float(q.get("percent_blocked") or 0), 1),
        "unique_domains": q.get("unique_domains", 0),
        "clients": (s.get("clients") or {}).get("active", 0),
        "gravity": (s.get("gravity") or {}).get("domains_being_blocked", 0),
        "top_blocked": [{"domain": d.get("domain"), "count": d.get("count")} for d in top],
        "updated": int(time.time()),
    }


# MakerOS eklenti kiti başlığı (her eklenti sayfasının <head>'i): kabuğun tema belirteçleri + dil.
# Kabuk içinde /shell/css/tokens.css ve /shell/js/plugin-kit.js gelir (renkler, koyu/açık, vurgu, dil; canlı eşitleme).
# Eklenti doğrudan açılırsa (kabuk yok) aşağıdaki yedek değerler ve küçük MK yedeği kullanılır.
KIT_HEAD = r"""<link rel=stylesheet href="/shell/css/tokens.css"><script src="/shell/js/plugin-kit.js"></script>
<script>if(!window.MK){const D={};window.MK={lang:(navigator.language||'tr').startsWith('en')?'en':'tr',locale:'tr-TR',dark:matchMedia('(prefers-color-scheme: dark)').matches,accent:'#f07a1a',
i18n(d){for(const l in d)D[l]=Object.assign(D[l]||{},d[l]);this.translate();return this},
t(k,v){let s=typeof k==='object'&&k?(k[this.lang]??k.tr):((D[this.lang]||{})[k]??(D.tr||{})[k]??k);if(v)s=String(s).replace(/\{(\w+)\}/g,(_,x)=>v[x]??'');return s},
translate(el=document){if(!el.querySelectorAll)return;for(const n of el.querySelectorAll('[data-t]'))n.textContent=this.t(n.dataset.t);for(const n of el.querySelectorAll('[data-t-placeholder]'))n.placeholder=this.t(n.dataset.tPlaceholder);for(const n of el.querySelectorAll('[data-t-title]'))n.title=this.t(n.dataset.tTitle)},
on(){return()=>{}},theme(){return{accent:this.accent,dark:this.dark,lang:this.lang,colors:{}}}};MK.locale=MK.lang==='en'?'en-GB':'tr-TR'}
// Sunucu hata mesajları da MakerOS dilinde gelsin.
window.mkFetch=(p,o={})=>fetch(p,{...o,headers:{...(o.headers||{}),'X-MakerOS-Lang':MK.lang}});</script>
<style>
:root{--a:var(--accent,#f07a1a);--on:var(--on-accent,#fff);--bg:var(--bg-window,#f7f7f9);--fg:var(--text,#1d1d21);--mut:var(--text-2,#5f5f68);
 --card:var(--bg-elevated,#fff);--field:var(--bg-field,#fff);--ctl:var(--bg-control,#fff);--bd:var(--separator,rgba(0,0,0,.1));--bd2:var(--border,rgba(0,0,0,.14));
 --ok:var(--success,#2fa84a);--bad:var(--danger,#d93542);--r:var(--radius-12,12px);--ff:var(--font-sans,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 var(--ff);-webkit-font-smoothing:antialiased}
h1{font-size:20px;margin:0 0 2px;letter-spacing:-.01em}.sub{color:var(--mut);font-size:12.5px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input,select{font:inherit;padding:7px 10px;border-radius:8px;border:1px solid var(--bd2);background:var(--field);color:var(--fg);min-width:0}
input[type=checkbox]{accent-color:var(--a)}input:focus,select:focus{outline:none;border-color:var(--a);box-shadow:0 0 0 3px color-mix(in srgb,var(--a) 30%,transparent)}
button{font:inherit;padding:7px 13px;border-radius:8px;border:0;background:var(--a);color:var(--on);cursor:pointer}
button.g{background:var(--ctl);color:var(--fg);border:1px solid var(--bd2)}button:disabled{opacity:.5;cursor:default}
.err{color:var(--bad);white-space:pre-wrap}.hide{display:none!important}label{font-size:13px;color:var(--mut)}
</style>"""


PAGE = r"""<!doctype html><html lang=tr><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Pi-hole</title>""" + KIT_HEAD + r"""<style>
body{padding:20px}.card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:16px;margin-top:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:14px}
.k{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:14px}.k b{display:block;font-size:24px;font-weight:650}.k span{color:var(--mut);font-size:12px}
input.f{flex:1}.pill{display:inline-flex;align-items:center;gap:8px;font-weight:600}.pill i{width:10px;height:10px;border-radius:50%;background:var(--mut)}
.on i{background:var(--ok)}.off i{background:var(--bad)}#err{margin-top:8px}
table{width:100%;border-collapse:collapse}td{padding:6px 0;border-bottom:1px solid var(--bd)}td:last-child{text-align:right;color:var(--mut)}
</style>
<div class=row style="justify-content:space-between"><div><h1>Pi-hole</h1><div class=sub id=src data-t=tagline></div></div>
<div class=row><span id=st class=pill><i></i><span>…</span></span><button class=g id=gear data-t=settings></button></div></div>
<div class="card hide" id=cfg>
 <div class=row><input class=f id=url data-t-placeholder=urlPh><input class=f id=pw type=password></div>
 <div class=row style="margin-top:10px"><label><input type=checkbox id=ins> <span data-t=insecure></span></label>
 <span style="flex:1"></span><button id=save data-t=save></button></div>
 <div class=sub style="margin-top:8px" data-t=pwNote></div>
</div>
<div class=err id=err></div>
<div id=main class=hide>
 <div class=grid>
  <div class=k><b id=q>–</b><span data-t=queries></span></div>
  <div class=k><b id=bl>–</b><span data-t=blocked></span></div>
  <div class=k><b id=pc>–</b><span data-t=percent></span></div>
  <div class=k><b id=gr>–</b><span data-t=gravity></span></div>
  <div class=k><b id=cl>–</b><span data-t=clients></span></div>
 </div>
 <div class=card><div class=row style="justify-content:space-between"><b data-t=blocking></b>
  <div class=row><button id=off5 class=g data-t=off5></button><button id=off class=g data-t=off></button><button id=on data-t=on></button></div></div>
  <div class=sub id=tm></div></div>
 <div class=card><b data-t=top></b><table id=top></table></div>
</div>
<script>
MK.i18n({
 tr:{tagline:'Ağ genelinde reklam engelleyici',settings:'Ayarlar',urlPh:'Pi-hole adresi (ör. http://192.168.1.2)',pwPh:'Parola',pwKept:'Parola (kayıtlı; değiştirmek için yazın)',
  insecure:'Kendinden imzalı HTTPS sertifikasına izin ver',save:'Kaydet ve bağlan',pwNote:'Parola cihazda ~/.config/makeros-plugins/pihole.json içinde saklanır; eklentiyi dışa aktarırken paylaşılmaz.',
  queries:'Toplam sorgu (24 sa)',blocked:'Engellenen',percent:'Engelleme oranı',gravity:'Engel listesindeki alan adı',clients:'Etkin istemci',blocking:'Engelleme',
  off5:'5 dk kapat',off:'Kapat',on:'Aç',top:'En çok engellenen alan adları',none:'Henüz yok',notSet:'Ayarlanmadı',stOn:'Engelleme açık',stOff:'Engelleme kapalı',stOther:'Durum: {s}',
  noConn:'Bağlantı yok',timer:'{n} sn sonra kendiliğinden açılacak'},
 en:{tagline:'Network-wide ad blocker',settings:'Settings',urlPh:'Pi-hole address (e.g. http://192.168.1.2)',pwPh:'Password',pwKept:'Password (saved; type to change)',
  insecure:'Allow self-signed HTTPS certificate',save:'Save and connect',pwNote:'The password is stored on this device in ~/.config/makeros-plugins/pihole.json and is not shared when exporting the plugin.',
  queries:'Total queries (24 h)',blocked:'Blocked',percent:'Blocked share',gravity:'Domains on blocklist',clients:'Active clients',blocking:'Blocking',
  off5:'Disable 5 min',off:'Disable',on:'Enable',top:'Top blocked domains',none:'None yet',notSet:'Not set up',stOn:'Blocking on',stOff:'Blocking off',stOther:'Status: {s}',
  noConn:'No connection',timer:'Re-enables automatically in {n} s'}});
const $=id=>document.getElementById(id),fmt=n=>Number(n||0).toLocaleString(MK.locale);let CFG={},LAST=null,ERR='';
async function api(p,b){const r=await mkFetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{cache:'no-store'});
 const d=await r.json().catch(()=>({error:'HTTP '+r.status}));if(!r.ok||d.ok===false)throw new Error(d.error||('HTTP '+r.status));return d}
function status(cls,t){$('st').className='pill '+cls;$('st').lastChild.textContent=t}
function draw(){$('pw').placeholder=MK.t(CFG.has_password?'pwKept':'pwPh');
 if(!CFG.url){$('cfg').classList.remove('hide');status('',MK.t('notSet'));return}$('src').textContent=CFG.url;
 if(ERR){status('off',MK.t('noConn'));$('err').textContent=ERR;return}const d=LAST;if(!d)return;$('err').textContent='';$('main').classList.remove('hide');
 $('q').textContent=fmt(d.queries);$('bl').textContent=fmt(d.blocked);$('pc').textContent=(MK.lang==='tr'?'%':'')+d.percent.toLocaleString(MK.locale)+(MK.lang==='tr'?'':'%');$('gr').textContent=fmt(d.gravity);$('cl').textContent=fmt(d.clients);
 const on=d.blocking==='enabled';status(on?'on':'off',on?MK.t('stOn'):d.blocking==='disabled'?MK.t('stOff'):MK.t('stOther',{s:d.blocking}));
 $('on').disabled=on;$('off').disabled=$('off5').disabled=!on;$('tm').textContent=d.timer?MK.t('timer',{n:Math.round(d.timer)}):'';
 $('top').innerHTML='';for(const t of d.top_blocked){const tr=$('top').insertRow();tr.insertCell().textContent=t.domain;tr.insertCell().textContent=fmt(t.count)}
 if(!d.top_blocked.length){const tr=$('top').insertRow();const c=tr.insertCell();c.className='sub';c.textContent=MK.t('none');tr.insertCell()}}
async function loadCfg(){CFG=await api('api/config');$('url').value=CFG.url||'';$('ins').checked=!!CFG.insecure;draw()}
async function tick(){if(!CFG.url)return;try{LAST=await api('api/summary');ERR=''}catch(e){ERR=e.message}draw()}
async function block(enabled,timer){try{await api('api/blocking',{enabled,timer});tick()}catch(e){$('err').textContent=e.message}}
$('gear').onclick=()=>$('cfg').classList.toggle('hide');
$('save').onclick=async()=>{$('save').disabled=true;try{const b={url:$('url').value,insecure:$('ins').checked};if($('pw').value)b.password=$('pw').value;
 await api('api/config',b);$('pw').value='';$('cfg').classList.add('hide');await loadCfg();tick()}catch(e){$('err').textContent=e.message}finally{$('save').disabled=false}};
$('on').onclick=()=>block(true,null);$('off').onclick=()=>block(false,null);$('off5').onclick=()=>block(false,300);
MK.on((_,ch)=>{if(ch.has('lang')){draw();tick()}});  // dil: metinler + sunucu mesajları; tema: CSS değişkenleri kendiliğinden
loadCfg().then(tick);setInterval(()=>{if(document.visibilityState==='visible')tick()},10000);
</script>"""



class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 << 10:
            raise ValueError("istek çok büyük")
        d = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(d, dict):
            raise ValueError("JSON nesnesi bekleniyor")
        return d

    def _path(self):
        _req.lang = "en" if (self.headers.get("X-MakerOS-Lang") or "").startswith("en") else "tr"
        return "/" + self.path.split("?")[0].strip("/")

    def do_GET(self):
        p = self._path()
        try:
            if p == "/health":
                c = load_conf()
                out = {"ok": True, "plugin": PLUGIN_ID, "configured": bool(c.get("url")), "connected": False}
                if out["configured"]:
                    try:
                        PI.call("GET", "/dns/blocking")
                        out["connected"] = True
                    except PiholeError as e:
                        out["error"] = str(e)
                return self._send(200, out)
            if p == "/api/config":
                c = load_conf()
                return self._send(200, {"url": c.get("url", ""), "has_password": bool(c.get("password")), "insecure": bool(c.get("insecure"))})
            if p == "/api/summary":
                return self._send(200, summary())
            if p == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return self._send(404, {"ok": False, "error": m("bulunamadı", "not found")})
        except PiholeError as e:
            return self._send(502, {"ok": False, "error": str(e)})

    def do_POST(self):
        p = self._path()
        try:
            b = self._body()
            if p == "/api/config":
                c = load_conf()
                url = norm_url(b.get("url"))
                if not url.startswith(("http://", "https://")) or len(url) > 300:
                    return self._send(400, {"ok": False, "error": m("Adres http:// veya https:// ile başlamalı.", "Address must start with http:// or https://.")})
                c["url"] = url
                if isinstance(b.get("password"), str):
                    c["password"] = b["password"]
                c["insecure"] = bool(b.get("insecure"))
                PI.logout()
                save_conf(c)
                PI.call("GET", "/dns/blocking")  # bağlantıyı dene; hata 502 olarak döner (ayar yine kaydedilir)
                return self._send(200, {"ok": True})
            if p == "/api/blocking":
                timer = b.get("timer")
                if timer is not None and (not isinstance(timer, (int, float)) or not 0 < timer <= 86400):
                    return self._send(400, {"ok": False, "error": m("timer 1–86400 saniye olmalı.", "timer must be 1–86400 seconds.")})
                d = PI.call("POST", "/dns/blocking", {"blocking": bool(b.get("enabled")), "timer": timer})
                return self._send(200, {"ok": True, "blocking": d.get("blocking"), "timer": d.get("timer")})
            return self._send(404, {"ok": False, "error": m("bulunamadı", "not found")})
        except PiholeError as e:
            return self._send(502, {"ok": False, "error": str(e)})
        except ValueError as e:
            return self._send(400, {"ok": False, "error": m(f"Geçersiz istek: {e}", f"Invalid request: {e}")})


if __name__ == "__main__":
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        print(f"Pi-hole eklentisi: port {PORT} kullanımda, zaten çalışan kopya kullanılıyor.", flush=True)
        sys.exit(0)
    print("Pi-hole eklentisi; port", PORT, flush=True)
    import signal

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # makerd "Durdur" → finally çalışsın
    try:
        srv.serve_forever()
    finally:
        PI.logout()  # Pi-hole'un sınırlı oturum yuvasını boşa harcama
