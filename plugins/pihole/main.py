"""Pi-hole eklentisi: ağ genelinde reklam engelleyici, Pi'nin içinde; arayüz Pi-hole'un kendi yönetim paneli (MakerOS temasıyla).

İki kip:
  local  (varsayılan) Pi-hole bu cihazda, rootless Podman kapsayıcısında çalışır (docker.io/pihole/pihole).
         DNS: cihazın ağ adresi :53 (yönlendiricide DNS olarak bu adresi verin). Yönetim paneli yalnız cihaz içinde
         (127.0.0.1:8053). Ayarlar/listeler ~/.local/share/makeros-plugins/pihole/ altında kalıcıdır. Eklenti durunca
         kapsayıcı da durur.
  remote Ağdaki başka bir Pi-hole'a (v6) bağlanır.

Arayüz: 0.0.0.0:8796 ters vekil Pi-hole panelini kökte sunar, gömmeyi engelleyen başlıkları kaldırır, MakerOS temasını ekler.
MakerOS'tan açılınca tek kullanımlık biletle panel oturumu (sid çerezi) kendiliğinden açılır; vekile doğrudan gelen
Pi-hole'un kendi giriş ekranını görür.

  127.0.0.1:<web_port> (MakerOS /plugin/pihole/):
    GET  /               panel (gömülü) + ayarlar
    GET  /health         {"ok", "mode", "state", "connected", "dns"?, "error"?}
    GET|POST /api/config {"mode", "url", "password"?, "insecure"}
    GET  /api/status     kapsayıcı durumu (pulling | starting | running | error) ve son günlük satırları
    GET  /api/ticket     tek kullanımlık giriş bileti
    POST /api/theme      kabuğun teması/dili (vekil panele uygular)
"""
import asyncio
import contextvars
import json
import os
import re
import secrets
import shutil
import signal
import ssl
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

PLUGIN_ID = os.environ.get("MAKEROS_PLUGIN_ID", "pihole")
PORT = int(os.environ.get("MAKEROS_PLUGIN_PORT", "8791"))
PROXY_PORT = 8796
LOCAL_WEB = 8053  # kapsayıcının paneli: yalnız 127.0.0.1
IMAGE = "docker.io/pihole/pihole:latest"
CNAME = "makeros-pihole"
CONF = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "makeros-plugins" / f"{PLUGIN_ID}.json"
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "makeros-plugins" / PLUGIN_ID
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade",
       "proxy-authorization", "proxy-authenticate", "host", "content-length"}
LANG = contextvars.ContextVar("lang", default="tr")


def m(tr, en):
    return en if LANG.get() == "en" else tr


class PiholeError(Exception):
    pass


# --- ayarlar -----------------------------------------------------------------

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


def conf():
    """Varsayılanlarla ayarlar; yerleşik kipin parolası ilk açılışta üretilir."""
    c = load_conf()
    changed = False
    if c.get("mode") not in ("local", "remote"):
        # Eski sürümden (yalnız uzak) gelenlerde adres varsa uzak kip korunur.
        c["mode"], changed = ("remote" if c.get("url") else "local"), True
    if not c.get("local_password"):
        c["local_password"], changed = secrets.token_urlsafe(18), True
    if changed:
        save_conf(c)
    return c


def norm_url(u):
    u = (u or "").strip().rstrip("/")
    if u and "://" not in u:
        u = "http://" + u
    for suf in ("/admin", "/api"):
        if u.endswith(suf):
            u = u[: -len(suf)]
    return u


def target(c):
    """(adres, parola, ssl bağlamı) — panelin ve API'nin tabanı."""
    if c.get("mode") == "remote":
        ctx = None
        if c.get("insecure"):
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        return norm_url(c.get("url")), c.get("password", ""), ctx
    return f"http://127.0.0.1:{LOCAL_WEB}", c["local_password"], None


# --- yerleşik Pi-hole (Podman) -----------------------------------------------------

STATE = {"state": "idle", "error": None, "dns": None, "log": []}
PROC = None
STOPPING = asyncio.Event()


def lan_ip():
    """Varsayılan rotanın kaynak adresi (yönlendiricide DNS olarak verilecek adres)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def log(line):
    line = line.rstrip()
    if line:
        STATE["log"] = (STATE["log"] + [line])[-40:]
        print(line, flush=True)


async def podman(*args):
    p = await asyncio.create_subprocess_exec("podman", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="replace")


async def run_local():
    """Kapsayıcıyı ön planda çalıştırır ve izler; çökerse 15 sn sonra yeniden başlatır."""
    global PROC
    if not shutil.which("podman"):
        STATE.update(state="error", error="Podman yok: yerleşik Pi-hole çalıştırılamaz (Ayarlar'dan uzak kipe geçin).")
        return
    (DATA / "etc-pihole").mkdir(parents=True, exist_ok=True)
    while not STOPPING.is_set() and conf().get("mode") == "local":
        c = conf()
        code, _ = await podman("image", "exists", IMAGE)
        if code != 0:
            STATE.update(state="pulling", error=None)
            log("Pi-hole imajı indiriliyor (~100 MB)…")
            code, out = await podman("pull", "-q", IMAGE)
            if code != 0:
                STATE.update(state="error", error="İmaj indirilemedi: " + out.strip()[-300:])
                await asyncio.sleep(30)
                continue
        ip = lan_ip()
        for with_dns in (True, False):
            args = ["run", "--rm", "--replace", "--name", CNAME, "-p", f"127.0.0.1:{LOCAL_WEB}:80/tcp",
                    "-e", f"TZ={os.environ.get('TZ') or 'Europe/Istanbul'}", "-e", f"FTLCONF_webserver_api_password={c['local_password']}",
                    "-e", "FTLCONF_dns_listeningMode=ALL", "-e", "FTLCONF_dns_upstreams=1.1.1.1;9.9.9.9",
                    "-v", f"{DATA / 'etc-pihole'}:/etc/pihole", "--cap-add", "NET_ADMIN", "--cap-add", "SYS_NICE"]
            if with_dns and ip:
                args += ["-p", f"{ip}:53:53/tcp", "-p", f"{ip}:53:53/udp"]
            STATE.update(state="starting", error=None, dns=ip if (with_dns and ip) else None)
            PROC = await asyncio.create_subprocess_exec("podman", *args, IMAGE, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            started = time.time()
            async for raw in PROC.stdout:
                text = raw.decode(errors="replace")
                log(text)
                if "port 53" in text and ("Permission denied" in text or "in use" in text):
                    STATE["error"] = "DNS (53) açılamadı; panel yine çalışır."
            await PROC.wait()
            if STOPPING.is_set() or conf().get("mode") != "local":
                return
            if with_dns and ip and time.time() - started < 20:
                continue  # 53 bağlanamadıysa yalnız panelle dene
            break
        STATE.update(state="error", error=STATE.get("error") or "Pi-hole kapsayıcısı durdu; yeniden başlatılıyor.")
        await asyncio.sleep(15)


async def watch_ready():
    """Panel yanıt verince durum 'running' olur."""
    while not STOPPING.is_set():
        if conf().get("mode") == "local" and STATE["state"] == "starting":
            try:
                async with SESSION.get(f"http://127.0.0.1:{LOCAL_WEB}/api/info/login", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status < 500:
                        STATE["state"] = "running"
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
        await asyncio.sleep(2)


# --- Pi-hole API (oturum) ---------------------------------------------------------

SESSION = None
PROXY = None


async def pihole_login(c):
    base, pw, ctx = target(c)
    if not base:
        raise PiholeError(m("Önce Pi-hole adresini girin.", "Enter the Pi-hole address first."))
    try:
        async with SESSION.post(base + "/api/auth", json={"password": pw}, ssl=ctx, timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise PiholeError(m(f"Pi-hole'a ulaşılamadı ({e.__class__.__name__}).", f"Cannot reach Pi-hole ({e.__class__.__name__}).")) from None
    s = (d or {}).get("session") or {}
    if not s.get("valid"):
        raise PiholeError(m("Pi-hole parolası yanlış.", "Wrong Pi-hole password."))
    return s.get("sid") or ""


# --- tema -------------------------------------------------------------------------

FONT = '"Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'


def theme_css(c):
    a = c.get("accent") if HEX_RE.match(str(c.get("accent", ""))) else "#f07a1a"
    dark = c.get("dark") is True
    bg, card, fg, mut, bd, side = (("#262629", "#323236", "#efeff3", "#a9a9b2", "rgba(255,255,255,.09)", "#2b2b2f") if dark
                                   else ("#f7f7f9", "#ffffff", "#1d1d21", "#5f5f68", "rgba(0,0,0,.1)", "#e9e9ee"))
    return f"""<style id="makeros-theme">
html,body,.content-wrapper,.main-footer,h1,h2,h3,h4,.box-title{{font-family:{FONT}!important}}
body,.wrapper,.content-wrapper,.right-side,.main-footer{{background:{bg}!important;color:{fg}!important}}
.box,.small-box,.info-box,.card,.panel,.modal-content,.dropdown-menu{{border-radius:12px!important;border:1px solid {bd}!important;box-shadow:none!important}}
.box,.info-box,.card,.panel,.modal-content,.dropdown-menu,.box-header,.box-footer{{background:{card}!important;color:{fg}!important}}
.main-header .navbar,.main-header .logo{{background:{a}!important;color:#fff!important;border:0!important}}
.main-sidebar,.left-side,.sidebar{{background:{side}!important}}
.sidebar-menu>li>a,.sidebar a,.sidebar-menu .header{{color:{fg}!important;background:transparent!important}}
.sidebar-menu>li.active>a,.sidebar-menu>li>a:hover,.sidebar-menu .treeview-menu>li.active>a{{background:color-mix(in srgb,{a} 14%,transparent)!important;border-left-color:{a}!important;color:{a}!important}}
.btn-primary,.bg-primary,.label-primary,.progress-bar,.btn-success,.bg-aqua,.bg-green{{background-color:{a}!important;border-color:{a}!important;color:#fff!important}}
a,.text-primary,.text-aqua{{color:{a}}}.box.box-primary,.box.box-info{{border-top:3px solid {a}!important}}
input,select,textarea,.form-control{{border-radius:8px!important;background:{card}!important;color:{fg}!important;border-color:{bd}!important}}
.text-muted,.help-block,small{{color:{mut}!important}}.table>tbody>tr>td,.table>thead>tr>th{{border-color:{bd}!important}}
</style>"""


# --- MakerOS bileti ------------------------------------------------------------------

TICKETS = {}


def new_ticket():
    now = time.time()
    for k in [k for k, v in TICKETS.items() if v < now]:
        del TICKETS[k]
    t = secrets.token_urlsafe(24)
    TICKETS[t] = now + 60
    return t


def take_ticket(t):
    return bool(t) and TICKETS.pop(t, 0) > time.time()


# --- ters vekil (Pi-hole paneli) ----------------------------------------------------

def strip_frame_blockers(h):
    h.pop("X-Frame-Options", None)
    csp = h.get("Content-Security-Policy")
    if csp:
        parts = [p for p in csp.split(";") if not p.strip().lower().startswith("frame-ancestors")]
        if any(p.strip() for p in parts):
            h["Content-Security-Policy"] = ";".join(parts)
        else:
            h.pop("Content-Security-Policy", None)


async def proxy(req):
    c = conf()
    base, _, ctx = target(c)
    if not base:
        return web.Response(status=503, text="Pi-hole adresi ayarlanmadı (MakerOS → Pi-hole → Ayarlar).", content_type="text/plain")
    ticket = req.query.get("makeros_ticket")
    if ticket:
        # MakerOS'tan: Pi-hole'a sunucu tarafında giriş yapılır, panel oturumu (sid) çerez olarak verilir.
        if not take_ticket(ticket):
            raise web.HTTPFound("/admin/")
        try:
            sid = await pihole_login(c)
        except PiholeError as e:
            return web.Response(status=502, text=str(e), content_type="text/plain")
        resp = web.HTTPFound("/admin/")
        if sid:
            resp.set_cookie("sid", sid, path="/", httponly=True, samesite="Lax")
        raise resp
    hdrs = {k: v for k, v in req.headers.items()
            if k.lower() not in HOP and not k.lower().startswith(("x-forwarded", "sec-websocket")) and k.lower() not in ("forwarded", "accept-encoding", "origin")}
    hdrs["Accept-Encoding"] = "identity"
    try:
        r = await PROXY.request(req.method, base + str(req.rel_url), headers=hdrs, data=req.content if req.body_exists else None,
                                allow_redirects=False, ssl=ctx, timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        state = STATE["state"] if c.get("mode") == "local" else "-"
        msg = {"pulling": "Pi-hole indiriliyor…", "starting": "Pi-hole başlatılıyor…"}.get(state, f"Pi-hole'a ulaşılamadı: {e.__class__.__name__}")
        return web.Response(status=503, text=msg, content_type="text/plain")
    try:
        out = {k: v for k, v in r.headers.items() if k.lower() not in HOP and k.lower() != "content-encoding"}
        strip_frame_blockers(out)
        loc = out.get("Location")
        if loc and loc.startswith(base):
            out["Location"] = loc[len(base):] or "/"
        if r.headers.get("Content-Type", "").startswith("text/html"):
            html = (await r.read()).decode("utf-8", errors="replace")
            css = theme_css(c)
            html = html.replace("</head>", css + "</head>", 1) if "</head>" in html else css + html
            out.pop("ETag", None)
            out["Cache-Control"] = "no-store"
            return web.Response(status=r.status, body=html.encode(), headers=out)
        resp = web.StreamResponse(status=r.status, headers=out)
        if r.content_length is not None:
            resp.content_length = r.content_length
        await resp.prepare(req)
        async for chunk in r.content.iter_chunked(1 << 16):
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        r.release()


async def root_redirect(req):
    if req.query.get("makeros_ticket"):
        return await proxy(req)
    raise web.HTTPFound("/admin/")


# --- MakerOS sayfası ---------------------------------------------------------------

KIT_HEAD = r"""<link rel=stylesheet href="/shell/css/tokens.css"><script src="/shell/js/plugin-kit.js"></script>
<script>if(!window.MK){const D={};window.MK={lang:(navigator.language||'tr').startsWith('en')?'en':'tr',locale:'tr-TR',dark:matchMedia('(prefers-color-scheme: dark)').matches,accent:'#f07a1a',
i18n(d){for(const l in d)D[l]=Object.assign(D[l]||{},d[l]);this.translate();return this},
t(k,v){let s=typeof k==='object'&&k?(k[this.lang]??k.tr):((D[this.lang]||{})[k]??(D.tr||{})[k]??k);if(v)s=String(s).replace(/\{(\w+)\}/g,(_,x)=>v[x]??'');return s},
translate(el=document){if(!el.querySelectorAll)return;for(const n of el.querySelectorAll('[data-t]'))n.textContent=this.t(n.dataset.t);for(const n of el.querySelectorAll('[data-t-placeholder]'))n.placeholder=this.t(n.dataset.tPlaceholder);for(const n of el.querySelectorAll('[data-t-title]'))n.title=this.t(n.dataset.tTitle)},
on(){return()=>{}},theme(){return{accent:this.accent,dark:this.dark,lang:this.lang,colors:{}}}};MK.locale=MK.lang==='en'?'en-GB':'tr-TR'}
window.mkFetch=(p,o={})=>fetch(p,{...o,headers:{...(o.headers||{}),'X-MakerOS-Lang':MK.lang}});</script>
<style>
:root{--a:var(--accent,#f07a1a);--on:var(--on-accent,#fff);--bg:var(--bg-window,#f7f7f9);--fg:var(--text,#1d1d21);--mut:var(--text-2,#5f5f68);
 --card:var(--bg-elevated,#fff);--field:var(--bg-field,#fff);--ctl:var(--bg-control,#fff);--bd:var(--separator,rgba(0,0,0,.1));--bd2:var(--border,rgba(0,0,0,.14));
 --ok:var(--success,#2fa84a);--bad:var(--danger,#d93542);--r:var(--radius-12,12px);--ff:var(--font-sans,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 var(--ff);-webkit-font-smoothing:antialiased}
.sub{color:var(--mut);font-size:12.5px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input,select{font:inherit;padding:7px 10px;border-radius:8px;border:1px solid var(--bd2);background:var(--field);color:var(--fg);min-width:0}
input[type=checkbox],input[type=radio]{accent-color:var(--a)}button{font:inherit;padding:7px 13px;border-radius:8px;border:0;background:var(--a);color:var(--on);cursor:pointer}
button.g{background:var(--ctl);color:var(--fg);border:1px solid var(--bd2)}button:disabled{opacity:.5}.err{color:var(--bad);white-space:pre-wrap}.hide{display:none!important}
label{font-size:13px;color:var(--mut)}
</style>"""

GEAR = '<svg width=15 height=15 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.8 stroke-linecap=round stroke-linejoin=round><circle cx=12 cy=12 r=3 /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'

PAGE = r"""<!doctype html><html lang=tr><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Pi-hole</title>""" + KIT_HEAD + r"""<style>
html,body{height:100%}#ph{position:fixed;inset:var(--tb,0px) 0 0 0;width:100%;height:calc(100% - var(--tb,0px));border:0;background:var(--bg)}
#gear{position:fixed;right:0;top:calc(50% + var(--tb,0px)/2);transform:translateY(-50%);z-index:5;width:22px;height:44px;padding:0 0 0 2px;border-radius:10px 0 0 10px;
 background:color-mix(in srgb,var(--card) 90%,transparent);color:var(--fg);border:1px solid var(--bd2);border-right:0;box-shadow:0 4px 14px rgba(0,0,0,.15);opacity:.45;transition:opacity .2s,width .2s}
#gear:hover{opacity:1;width:30px}#panel{position:fixed;inset:0;z-index:10;background:rgba(0,0,0,.35);display:grid;place-items:center;padding:16px}
.box{background:var(--bg);border:1px solid var(--bd2);border-radius:16px;padding:20px;width:min(600px,100%);max-height:100%;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.3)}
h2{margin:0 0 4px;font-size:17px}h3{margin:16px 0 8px;font-size:13px}input.f{flex:1}.center{position:fixed;inset:0;display:grid;place-items:center;text-align:center;padding:24px}
.dns{margin-top:12px;padding:10px 12px;border-radius:10px;background:var(--card);border:1px solid var(--bd)}
pre{max-width:min(720px,90vw);max-height:160px;overflow:auto;background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:8px;font-size:11px;white-space:pre-wrap;text-align:left}
</style>
<iframe id=ph class=hide referrerpolicy=no-referrer></iframe>
<div id=wait class="center"><div><div id=wt class=sub></div><pre id=wlog class=hide></pre></div></div>
<button id=gear data-t-title=settings>""" + GEAR + r"""</button>
<div id=panel class=hide><div class=box>
 <h2>Pi-hole</h2><div class=sub data-t=intro></div>
 <h3 data-t=modeT></h3>
 <div class=row><label><input type=radio name=mode value=local> <span data-t=local></span></label></div>
 <div class=row style="margin-top:6px"><label><input type=radio name=mode value=remote> <span data-t=remote></span></label></div>
 <div id=rem class=hide style="margin-top:8px">
  <div class=row><input class=f id=url data-t-placeholder=urlPh></div>
  <div class=row style="margin-top:8px"><input class=f id=pw type=password></div>
  <div class=row style="margin-top:8px"><label><input type=checkbox id=ins> <span data-t=insecure></span></label></div>
 </div>
 <div id=loc class=dns></div>
 <div class=row style="margin-top:16px;justify-content:flex-end"><span class=err id=msg style="flex:1"></span><button class=g id=close data-t=close></button><button id=save data-t=save></button></div>
</div></div>
<script>
MK.i18n({
 tr:{settings:'Ayarlar',intro:'Ağ genelinde reklam ve izleyici engelleyici.',modeT:'Çalışma kipi',local:'Bu cihazda çalıştır (önerilen)',remote:"Ağdaki başka bir Pi-hole'a bağlan",
  urlPh:'Pi-hole adresi (ör. http://192.168.1.2)',pwPh:'Parola',pwKept:'Parola kayıtlı (değiştirmek için yazın)',insecure:'Kendinden imzalı HTTPS sertifikasına izin ver',
  close:'Kapat',save:'Kaydet',pulling:'Pi-hole indiriliyor (ilk açılış, ~100 MB)…',starting:'Pi-hole başlatılıyor…',error:'Pi-hole çalışmıyor',idle:'Bekleniyor…',running:'Çalışıyor',loading:'Pi-hole yükleniyor…',
  dnsOn:'Ağ genelinde engelleme için yönlendiricinizde (modem) DNS sunucusu olarak <b>{ip}</b> adresini girin. Tek bir cihaz için o cihazın DNS ayarına da girebilirsiniz.',
  dnsOff:'DNS (53) açılamadı; panel çalışıyor ama bu cihaz DNS olarak kullanılamıyor.',state:'Durum: {s}',https:'MakerOS HTTPS ile açıldı; Pi-hole paneli (HTTP) gömülemez:'},
 en:{settings:'Settings',intro:'Network-wide ad and tracker blocker.',modeT:'Mode',local:'Run on this device (recommended)',remote:'Connect to another Pi-hole on the network',
  urlPh:'Pi-hole address (e.g. http://192.168.1.2)',pwPh:'Password',pwKept:'Password saved (type to change)',insecure:'Allow self-signed HTTPS certificate',
  close:'Close',save:'Save',pulling:'Downloading Pi-hole (first start, ~100 MB)…',starting:'Starting Pi-hole…',error:'Pi-hole is not running',idle:'Waiting…',running:'Running',loading:'Loading Pi-hole…',
  dnsOn:"For network-wide blocking, set <b>{ip}</b> as the DNS server on your router. For a single device, set it in that device's DNS settings.",
  dnsOff:'DNS (53) could not be opened; the dashboard works but this device cannot be used as DNS.',state:'Status: {s}',https:'MakerOS was opened over HTTPS; the Pi-hole dashboard (HTTP) cannot be embedded:'}});
const $=id=>document.getElementById(id);let CFG={},ST={},opened=false;
async function api(p,b){const r=await mkFetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{cache:'no-store'});
 const d=await r.json().catch(()=>({error:'HTTP '+r.status}));if(!r.ok||d.ok===false)throw new Error(d.error||('HTTP '+r.status));return d}
function embedMode(){try{if(parent===window)return'standalone';const d=parent.document.documentElement;return d.dataset.ui==='web'?'page':d.dataset.layout==='mobile'?'mobile':'window'}catch{return'standalone'}}
if(embedMode()==='window'){let h=38;try{h=parseInt(getComputedStyle(parent.document.documentElement).getPropertyValue('--mk-titlebar-height'))||38}catch{}document.documentElement.style.setProperty('--tb',h+'px')}
const phURL=()=>`http://${location.hostname}:${CFG.proxy_port}/`;
async function pushTheme(){const th=MK.theme();try{await api('api/theme',{accent:th.accent,dark:th.dark,lang:MK.lang})}catch{}}
async function openPH(){if(location.protocol==='https:'){$('wt').innerHTML='';$('wt').append(MK.t('https')+' ',Object.assign(document.createElement('a'),{href:phURL(),target:'_blank',textContent:phURL()}));return}
 opened=true;let t=null;try{t=(await api('api/ticket')).ticket}catch{}const f=$('ph');f.onload=()=>{$('wait').classList.add('hide');f.classList.remove('hide')};
 f.src=phURL()+(t?'?makeros_ticket='+encodeURIComponent(t):'')}
function drawLoc(){const l=$('loc');if(CFG.mode!=='local'){l.classList.add('hide');return}l.classList.remove('hide');
 const esc=s=>String(s).replace(/[<&]/g,c=>c==='<'?'&lt;':'&amp;');
 l.innerHTML=(ST.dns?MK.t('dnsOn',{ip:esc(ST.dns)}):(ST.state==='running'?MK.t('dnsOff'):''))+`<div class=sub style="margin-top:6px">${esc(MK.t('state',{s:MK.t(ST.state||'idle')}))}${ST.error?' · '+esc(ST.error):''}</div>`}
async function poll(){try{ST=await api('api/status')}catch(e){ST={state:'error',error:e.message}}
 if(CFG.mode==='local'&&ST.state!=='running'){$('wait').classList.remove('hide');$('ph').classList.add('hide');opened=false;$('wt').textContent=MK.t(ST.state||'idle');
  const lg=$('wlog');lg.textContent=(ST.log||[]).slice(-12).join('\n');lg.classList.toggle('hide',!(ST.log||[]).length)}
 else if(!opened)openPH();
 drawLoc();setTimeout(poll,opened?10000:2000)}
function panel(show){$('panel').classList.toggle('hide',!show);if(!show)return;document.querySelectorAll('[name=mode]').forEach(r=>r.checked=r.value===CFG.mode);
 $('rem').classList.toggle('hide',CFG.mode!=='remote');$('url').value=CFG.url||'';$('ins').checked=!!CFG.insecure;$('pw').placeholder=MK.t(CFG.has_password?'pwKept':'pwPh');$('msg').textContent='';drawLoc()}
document.querySelectorAll('[name=mode]').forEach(r=>r.onchange=()=>$('rem').classList.toggle('hide',!(r.value==='remote'&&r.checked)));
$('gear').onclick=()=>panel(true);$('close').onclick=()=>panel(false);
$('save').onclick=async()=>{$('save').disabled=true;$('msg').textContent='';try{const mode=document.querySelector('[name=mode]:checked')?.value||'local';
 const b={mode,url:$('url').value,insecure:$('ins').checked};if($('pw').value)b.password=$('pw').value;await api('api/config',b);$('pw').value='';
 CFG=await api('api/config');panel(false);opened=false;$('ph').removeAttribute('src')}catch(e){$('msg').textContent=e.message}finally{$('save').disabled=false}};
MK.on(()=>{pushTheme().then(()=>{if(opened)try{$('ph').src=$('ph').src.split('?')[0]}catch{}});drawLoc()});
$('wt').textContent=MK.t('loading');
(async()=>{CFG=await api('api/config');await pushTheme();if(CFG.mode==='remote'&&!CFG.url){$('wait').classList.add('hide');panel(true)}poll()})();
</script>"""


# --- MakerOS uç noktaları -------------------------------------------------------------

def j(d, status=200):
    return web.json_response(d, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False), headers={"Cache-Control": "no-store"})


@web.middleware
async def errors(req, handler):
    LANG.set("en" if req.headers.get("X-MakerOS-Lang", "").startswith("en") else "tr")
    try:
        return await handler(req)
    except PiholeError as e:
        return j({"ok": False, "error": str(e)}, 502)
    except (ValueError, json.JSONDecodeError) as e:
        return j({"ok": False, "error": m(f"Geçersiz istek: {e}", f"Invalid request: {e}")}, 400)


async def body(req):
    if (req.content_length or 0) > 64 << 10:
        raise ValueError("request too large")
    d = await req.json()
    if not isinstance(d, dict):
        raise ValueError("JSON object expected")
    return d


async def h_index(_):
    return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})


async def h_health(_):
    c = conf()
    out = {"ok": True, "plugin": PLUGIN_ID, "mode": c["mode"], "connected": False}
    if c["mode"] == "local":
        out.update(state=STATE["state"], dns=STATE["dns"])
    else:
        out["configured"] = bool(c.get("url"))
    try:
        await pihole_login(c)
        out["connected"] = True
    except PiholeError as e:
        out["error"] = str(e)
    return j(out)


async def h_config_get(_):
    c = conf()
    return j({"mode": c["mode"], "url": c.get("url", ""), "has_password": bool(c.get("password")), "insecure": bool(c.get("insecure")), "proxy_port": PROXY_PORT})


async def h_config_post(req):
    b = await body(req)
    c = conf()
    mode = b.get("mode", c["mode"])
    if mode not in ("local", "remote"):
        raise ValueError("mode local|remote")
    if mode == "remote":
        url = norm_url(b.get("url"))
        if not url.startswith(("http://", "https://")) or len(url) > 300:
            return j({"ok": False, "error": m("Adres http:// veya https:// ile başlamalı.", "Address must start with http:// or https://.")}, 400)
        c["url"] = url
        if isinstance(b.get("password"), str) and b["password"]:
            c["password"] = b["password"]
        c["insecure"] = bool(b.get("insecure"))
    old = c["mode"]
    c["mode"] = mode
    save_conf(c)
    if mode == "remote":
        if old == "local":
            await stop_local()
        await pihole_login(c)  # bağlantıyı dene (hata 502 olarak döner; ayar kaydedildi)
    elif old == "remote":
        start_local()
    return j({"ok": True})


async def h_status(_):
    c = conf()
    if c["mode"] != "local":
        return j({"ok": True, "state": "running", "mode": "remote"})
    return j({"ok": True, "mode": "local", **STATE})


async def h_ticket(_):
    return j({"ok": True, "ticket": new_ticket()})


async def h_theme(req):
    b = await body(req)
    c = load_conf()
    acc, dark = b.get("accent"), b.get("dark")
    new = {"accent": acc if isinstance(acc, str) and HEX_RE.match(acc) else c.get("accent"), "dark": dark if isinstance(dark, bool) else None}
    if any(c.get(k) != v for k, v in new.items()):
        c.update(new)
        save_conf(c)
    return j({"ok": True})


# --- yaşam döngüsü ------------------------------------------------------------------

LOCAL_TASK = None


def start_local():
    global LOCAL_TASK
    if LOCAL_TASK is None or LOCAL_TASK.done():
        LOCAL_TASK = asyncio.get_running_loop().create_task(run_local())


async def stop_local():
    if PROC is not None and PROC.returncode is None:
        await podman("stop", "-t", "8", CNAME)
    STATE.update(state="idle", dns=None)


async def bind(runner, host, port, tries=30):
    for i in range(tries):
        try:
            await web.TCPSite(runner, host, port).start()
            return True
        except OSError:
            if i == tries - 1:
                return False
            await asyncio.sleep(0.5)


async def main():
    global SESSION, PROXY
    SESSION = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    PROXY = aiohttp.ClientSession(auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar())
    app = web.Application(middlewares=[errors])
    r = app.router
    r.add_get("/", h_index)
    r.add_get("/health", h_health)
    r.add_get("/api/config", h_config_get)
    r.add_post("/api/config", h_config_post)
    r.add_get("/api/status", h_status)
    r.add_get("/api/ticket", h_ticket)
    r.add_post("/api/theme", h_theme)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    if not await bind(runner, "127.0.0.1", PORT):
        print(f"Pi-hole eklentisi: port {PORT} kullanımda.", flush=True)
        return
    papp = web.Application(client_max_size=16 << 20)
    papp.router.add_get("/", root_redirect)
    papp.router.add_route("*", "/{tail:.+}", proxy)
    prunner = web.AppRunner(papp, access_log=None, shutdown_timeout=1)
    await prunner.setup()
    if not await bind(prunner, os.environ.get("MAKEROS_PIHOLE_PROXY_HOST", "0.0.0.0"), PROXY_PORT):
        print(f"Pi-hole panel vekili: {PROXY_PORT} açılamadı", flush=True)
    print(f"Pi-hole eklentisi; port {PORT}, panel vekili :{PROXY_PORT}", flush=True)
    if conf()["mode"] == "local":
        start_local()
    loop = asyncio.get_running_loop()
    watcher = loop.create_task(watch_ready())
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, STOPPING.set)
    await STOPPING.wait()
    watcher.cancel()
    await stop_local()
    await prunner.cleanup()
    await runner.cleanup()
    await SESSION.close()
    await PROXY.close()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
