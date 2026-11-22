"""Home Assistant eklentisi: Home Assistant'ın kendi arayüzü MakerOS penceresinde, MakerOS temasıyla;
seçilen varlıklar masaüstünde widget (kısayol) olarak.

Home Assistant'ı kurmaz; ağdaki bir Home Assistant'a bağlanır. İki dinleyici vardır:

1. 127.0.0.1:<web_port> (MakerOS /plugin/homeassistant/ üzerinden, oturum doğrulamalı):
     GET  /              Home Assistant arayüzü (tam ekran) + ayarlar
     GET  /widget        masaüstü widget'ı (seçili varlıklar, aç/kapat)
     GET  /health        {"ok", "configured", "connected", "version"?, "proxy": {...}}
     GET|POST /api/config     adres, erişim belirteci, vekil portu
     GET  /api/states    varlıklar (REST API, erişim belirteciyle)
     POST /api/call      {"entity_id", "action"}  (toggle | turn_on | press | open | close | stop | lock | unlock …)
     GET|POST /api/widget     widget'ta gösterilecek varlık kimlikleri
     POST /api/theme     {"accent": "#rrggbb", "dark": bool|null}  kabuğun teması (vekil arayüze uygular)

2. 0.0.0.0:<proxy_port> (varsayılan 8795): Home Assistant arayüzüne ters vekil.
   Home Assistant arayüzü kök yola bağlıdır (/frontend_latest, /api/websocket, /auth); alt yolda
   (/plugin/...) çalışamadığı için ayrı bir portta, kökte sunulur. Vekil kimlik bilgisi eklemez: girişi
   Home Assistant'ın kendi ekranı yapar (bir kez; "beni hatırla"). Vekil yalnız:
     * gömmeyi engelleyen başlıkları (X-Frame-Options, CSP frame-ancestors) kaldırır,
     * HTML sayfalarına MakerOS tema değişkenlerini ekler (vurgu rengi, yazı tipi, köşe yuvarlaklığı, koyu/açık),
     * WebSocket bağlantısını (/api/websocket) taşır.

Ayarlar ~/.config/makeros-plugins/homeassistant.json (0600) dosyasındadır (dışa aktarmaya girmez).
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
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

PLUGIN_ID = os.environ.get("MAKEROS_PLUGIN_ID", "homeassistant")
PORT = int(os.environ.get("MAKEROS_PLUGIN_PORT", "8792"))
DEFAULT_PROXY_PORT = 8795
CONF = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "makeros-plugins" / f"{PLUGIN_ID}.json"
UA = "MakerOS-homeassistant-eklentisi/1.1"
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

TOGGLE = {"light", "switch", "fan", "input_boolean", "automation", "siren", "humidifier", "media_player"}
ACTIONS = {
    **{d: ["toggle"] for d in TOGGLE},
    "scene": ["turn_on"],
    "script": ["turn_on"],
    "button": ["press"],
    "input_button": ["press"],
    "cover": ["open", "stop", "close"],
    "lock": ["lock", "unlock"],
    "vacuum": ["start", "return_to_base"],
}
SERVICE = {
    "toggle": "toggle", "turn_on": "turn_on", "turn_off": "turn_off", "press": "press",
    "open": "open_cover", "close": "close_cover", "stop": "stop_cover",
    "lock": "lock", "unlock": "unlock", "start": "start", "return_to_base": "return_to_base",
}
HIDDEN = {"zone", "persistent_notification", "update", "tts", "conversation", "event", "stt", "wake_word", "todo", "image", "ai_task"}
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade",
       "proxy-authorization", "proxy-authenticate", "host", "content-length"}


class HAError(Exception):
    pass


LANG = contextvars.ContextVar("lang", default="tr")  # istek başına MakerOS dili (X-MakerOS-Lang)


def m(tr, en):
    return en if LANG.get() == "en" else tr


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


def norm_url(u):
    u = (u or "").strip().rstrip("/")
    if u and "://" not in u:
        u = "http://" + u
    if u.endswith("/api"):
        u = u[:-4]
    return u


def proxy_port(c=None):
    p = (c or load_conf()).get("proxy_port") or DEFAULT_PROXY_PORT
    return int(p) if isinstance(p, int) and 1024 <= p <= 65535 else DEFAULT_PROXY_PORT


def ssl_ctx(c):
    if not c.get("insecure"):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    return ctx


# --- yerleşik Home Assistant (bu cihazda, rootless Podman) ---------------------------
# Kip "local": ghcr.io/home-assistant/home-assistant kapsayıcısı ana makine ağıyla çalışır (127.0.0.1:8123).
# Hazır olunca ilk kurulumu eklenti yapar (tek kullanıcı: MakerOS); belirteç gerekmez. Eklenti durunca kapsayıcı da durur.
LOCAL_URL = "http://127.0.0.1:8123"
HA_IMAGE = "ghcr.io/home-assistant/home-assistant:stable"
HA_NAME = "makeros-homeassistant"
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "makeros-plugins" / PLUGIN_ID
LSTATE = {"state": "idle", "error": None, "log": []}
LPROC = None
STOPPING = asyncio.Event()


def mode_of(c):
    m_ = c.get("mode")
    if m_ in ("local", "remote"):
        return m_
    return "remote" if c.get("url") and norm_url(c.get("url")) != LOCAL_URL else "local"


def llog(line):
    line = line.rstrip()
    if line:
        LSTATE["log"] = (LSTATE["log"] + [line[-300:]])[-40:]
        print(line, flush=True)


async def podman(*args):
    p = await asyncio.create_subprocess_exec("podman", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="replace")


async def run_local():
    global LPROC
    if not shutil.which("podman"):
        LSTATE.update(state="error", error="Podman yok: yerleşik Home Assistant çalıştırılamaz (Ayarlar'dan uzak kipe geçin).")
        return
    (DATA / "config").mkdir(parents=True, exist_ok=True)
    while not STOPPING.is_set() and mode_of(load_conf()) == "local":
        code, _ = await podman("image", "exists", HA_IMAGE)
        if code != 0:
            LSTATE.update(state="pulling", error=None)
            llog("Home Assistant imajı indiriliyor (~600 MB, birkaç dakika)…")
            code, out = await podman("pull", "-q", HA_IMAGE)
            if code != 0:
                LSTATE.update(state="error", error="İmaj indirilemedi: " + out.strip()[-300:])
                await asyncio.sleep(30)
                continue
        LSTATE.update(state="starting", error=None)
        LPROC = await asyncio.create_subprocess_exec(
            "podman", "run", "--rm", "--replace", "--name", HA_NAME, "--network", "host",
            "-e", f"TZ={os.environ.get('TZ') or 'Europe/Istanbul'}", "-v", f"{DATA / 'config'}:/config", HA_IMAGE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async for raw in LPROC.stdout:
            llog(raw.decode(errors="replace"))
        await LPROC.wait()
        if STOPPING.is_set() or mode_of(load_conf()) != "local":
            return
        LSTATE.update(state="error", error="Home Assistant kapsayıcısı durdu; yeniden başlatılıyor.")
        await asyncio.sleep(15)


async def watch_local():
    """HA yanıt verince: ilk kurulumu MakerOS ile yap (gerekirse), durum 'running'."""
    while not STOPPING.is_set():
        c = load_conf()
        if mode_of(c) == "local" and LSTATE["state"] in ("starting", "onboarding"):
            try:
                async with SESSION.get(LOCAL_URL + "/api/onboarding", timeout=aiohttp.ClientTimeout(total=4)) as r:
                    up = r.status < 500
            except (aiohttp.ClientError, asyncio.TimeoutError):
                up = False
            if up:
                c["url"] = LOCAL_URL
                save_conf(c)
                try:
                    if await onboarding_pending(c):
                        LSTATE["state"] = "onboarding"
                        await onboard_as_makeros(load_conf())
                    cc = load_conf()
                    if cc.get("token") and cc.get("core_configured") != (cc.get("lang") or "tr"):
                        await configure_core()
                    if load_conf().get("token"):
                        LSTATE.update(state="running", error=None)
                except HAError as e:
                    LSTATE["error"] = str(e)
        await asyncio.sleep(3)


LOCAL_TASK = None


def start_local():
    global LOCAL_TASK
    if LOCAL_TASK is None or LOCAL_TASK.done():
        LOCAL_TASK = asyncio.get_running_loop().create_task(run_local())


async def stop_local():
    if LPROC is not None and LPROC.returncode is None:
        await podman("stop", "-t", "15", HA_NAME)
    LSTATE.update(state="idle")


# --- REST istemcisi (widget ve sağlık denetimi) ----------------------------------

SESSION: aiohttp.ClientSession | None = None  # REST (yanıtlar açılır)
PROXY: aiohttp.ClientSession | None = None  # arayüz vekili (baytlar olduğu gibi)


async def ha(method, path, body=None, timeout=10):
    c = load_conf()
    url, token = norm_url(c.get("url")), c.get("token", "")
    if not url or not token:
        raise HAError(m("Önce Home Assistant adresini ve erişim belirtecini girin.", "Enter the Home Assistant address and access token first."))
    try:
        async with SESSION.request(method, url + "/api" + path, json=body, ssl=ssl_ctx(c), timeout=aiohttp.ClientTimeout(total=timeout),
                                   headers={"Authorization": f"Bearer {token}", "User-Agent": UA}) as r:
            if r.status == 401:
                raise HAError(m("Erişim belirteci geçersiz veya süresi dolmuş.", "Access token is invalid or expired."))
            if r.status == 404:
                raise HAError(m("Bulunamadı: adres bir Home Assistant mı, varlık hâlâ var mı?", "Not found: is this a Home Assistant, does the entity still exist?"))
            if r.status >= 400:
                raise HAError(m(f"Home Assistant HTTP {r.status} döndü. {(await r.text())[:200]}", f"Home Assistant returned HTTP {r.status}. {(await r.text())[:200]}"))
            return await r.json(content_type=None) if r.content_length != 0 else None
    except aiohttp.ClientError as e:
        raise HAError(m(f"Home Assistant'a ulaşılamadı ({e.__class__.__name__}).", f"Cannot reach Home Assistant ({e.__class__.__name__}).")) from None
    except asyncio.TimeoutError:
        raise HAError(m("Home Assistant yanıt vermedi (zaman aşımı).", "Home Assistant did not respond (timeout).")) from None
    except ValueError:
        raise HAError(m("Yanıt JSON değil: adres bir Home Assistant mı?", "Response is not JSON: is this a Home Assistant address?")) from None


def entity(s):
    eid = s.get("entity_id", "")
    dom = eid.split(".", 1)[0]
    a = s.get("attributes") or {}
    return {"id": eid, "domain": dom, "name": a.get("friendly_name") or eid, "state": s.get("state"),
            "unit": a.get("unit_of_measurement") or "", "actions": ACTIONS.get(dom, [])}


async def states():
    cfg = await ha("GET", "/config") or {}
    raw = await ha("GET", "/states") or []
    ents = [entity(s) for s in raw if isinstance(s, dict) and s.get("entity_id", "").split(".", 1)[0] not in HIDDEN]
    ents.sort(key=lambda e: (not e["actions"], e["domain"], e["name"].lower()))
    return {"ok": True, "location": cfg.get("location_name", ""), "version": cfg.get("version", ""), "entities": ents}


async def call(eid, action):
    if not isinstance(eid, str) or not ENTITY_RE.match(eid):
        raise ValueError(m("entity_id geçersiz", "invalid entity_id"))
    dom = eid.split(".", 1)[0]
    allowed = set(ACTIONS.get(dom, [])) | ({"turn_on", "turn_off"} if dom in TOGGLE else set())
    if action not in allowed:
        raise ValueError(m(f"{dom} için '{action}' eylemi yok", f"no '{action}' action for {dom}"))
    svc_dom = "homeassistant" if action in ("toggle", "turn_on", "turn_off") and dom in TOGGLE else dom
    await ha("POST", f"/services/{svc_dom}/{SERVICE[action]}", {"entity_id": eid})
    st = await ha("GET", f"/states/{eid}")
    return {"ok": True, "entity": entity(st) if isinstance(st, dict) else None}


def widget_ids(c=None):
    ids = (c or load_conf()).get("widget_entities") or []
    return [i for i in ids if isinstance(i, str) and ENTITY_RE.match(i)][:12]


# --- tema ve dil (MakerOS ile eşitlenir) ---------------------------------------------
# Eklenti sayfası kabuğun gerçek paletini (MK.theme(): tokens.css değerleri, vurgu, koyu/açık) ve dilini POST /api/theme ile
# bildirir; vekil Home Assistant sayfalarına bunları CSS değişkeni olarak ekler. Değişince sayfa yenilenmeden postMessage ile uygulanır.

COLOR_RE = re.compile(r"^[#a-zA-Z0-9(),.%\s-]{1,48}$")
# Kabuk henüz bildirmediyse: MakerOS tokens.css değerleri.
PALETTE = {
    False: {"bg-window": "#f7f7f9", "bg-elevated": "#ffffff", "bg-sidebar": "#e9e9ee", "bg-content": "#ffffff", "text": "#1d1d21", "text-2": "#5f5f68",
            "text-3": "#8e8e96", "separator": "rgba(0,0,0,.1)", "border": "rgba(0,0,0,.14)", "success": "#2fa84a", "danger": "#d93542", "warning": "#e8a317", "on-accent": "#fff"},
    True: {"bg-window": "#262629", "bg-elevated": "#323236", "bg-sidebar": "#2b2b2f", "bg-content": "#1e1e21", "text": "#efeff3", "text-2": "#a9a9b2",
           "text-3": "#7c7c85", "separator": "rgba(255,255,255,.09)", "border": "rgba(255,255,255,.14)", "success": "#3cc15a", "danger": "#ff5a63", "warning": "#f5b83d", "on-accent": "#fff"},
}
FONT = '"Inter", system-ui, -apple-system, "SF Pro Text", "Segoe UI", Roboto, sans-serif'


def theme_text(c):
    """Home Assistant'a uygulanan CSS (yalnız metin; <style id=makeros-theme> içine)."""
    acc = c.get("accent") if HEX_RE.match(str(c.get("accent", ""))) else "#f07a1a"
    base = {
        "primary-color": acc, "accent-color": acc, "dark-primary-color": f"color-mix(in srgb, {acc} 80%, black)",
        "light-primary-color": f"color-mix(in srgb, {acc} 35%, white)", "sidebar-selected-icon-color": acc,
        "sidebar-selected-text-color": acc, "switch-checked-color": acc, "switch-checked-button-color": acc,
        "switch-checked-track-color": acc, "state-icon-active-color": acc, "mdc-theme-primary": acc, "mdc-theme-secondary": acc,
        "ha-font-family-body": FONT, "ha-font-family-heading": FONT, "paper-font-common-base_-_font-family": FONT,
        "paper-font-body1_-_font-family": FONT, "mdc-typography-font-family": FONT, "primary-font-family": FONT,
        "ha-card-border-radius": "12px", "ha-dialog-border-radius": "16px", "mdc-shape-small": "8px", "mdc-shape-medium": "12px",
        "ha-card-box-shadow": "none", "ha-card-border-width": "1px",
    }
    # Yeni HA bileşenlerinin (ha-button vb.) birincil paleti: 40 = dolu düğme; vurgu renginden türetilir.
    for k, (mix, with_) in {"05": (15, "black"), "10": (30, "black"), "20": (45, "black"), "30": (70, "black"), "40": (100, "black"),
                           "50": (85, "white"), "60": (70, "white"), "70": (55, "white"), "80": (35, "white"), "90": (18, "white"), "95": (9, "white")}.items():
        base[f"ha-color-primary-{k}"] = acc if mix == 100 else f"color-mix(in srgb, {acc} {mix}%, {with_})"

    def surface(p):
        return {"primary-background-color": p["bg-window"], "secondary-background-color": p["bg-sidebar"],
                "card-background-color": p["bg-elevated"], "ha-card-background": p["bg-elevated"], "ha-card-border-color": p["separator"],
                "sidebar-background-color": p["bg-sidebar"], "app-header-background-color": p["bg-window"], "app-header-text-color": p["text"],
                "primary-text-color": p["text"], "secondary-text-color": p["text-2"], "disabled-text-color": p["text-3"],
                "divider-color": p["separator"], "sidebar-text-color": p["text"], "sidebar-icon-color": p["text-2"],
                "error-color": p["danger"], "success-color": p["success"], "warning-color": p["warning"], "text-primary-color": p["on-accent"],
                "mdc-dialog-scrim-color": "rgba(0,0,0,.35)"}

    decl = lambda d: ";".join(f"--{k}:{v}!important" for k, v in d.items())
    dark = c.get("dark")
    if isinstance(dark, bool):
        pal = {**PALETTE[dark], **{k: v for k, v in (c.get("palette") or {}).items() if k in PALETTE[dark] and COLOR_RE.match(str(v))}}
        modes = f"html{{{decl(surface(pal))};color-scheme:{'dark' if dark else 'light'}}}"
    else:
        modes = (f"@media(prefers-color-scheme:light){{html{{{decl(surface(PALETTE[False]))}}}}}"
                 f"@media(prefers-color-scheme:dark){{html{{{decl(surface(PALETTE[True]))}}}}}")
    return f"html{{{decl(base)}}}{modes}html,body{{font-family:{FONT}!important}}"


def theme_head(c):
    """HA sayfasının <head> başına: dil (Home Assistant localStorage "selectedLanguage") ve canlı eşitleme dinleyicisi."""
    lang = c.get("lang") if c.get("lang") in ("tr", "en") else "tr"
    return ("<script>(function(){var L=" + json.dumps(lang) + ";try{if(JSON.parse(localStorage.getItem('selectedLanguage')||'null')!==L)"
            "localStorage.setItem('selectedLanguage',JSON.stringify(L))}catch(e){}"
            # MakerOS eklenti sayfası (aynı ana makine) tema/dil değişince haber verir.
            "addEventListener('message',function(e){var d=e.data;if(!d||d.mk!=='ha-theme')return;try{if(new URL(e.origin).hostname!==location.hostname)return}catch(x){return}"
            "var s=document.getElementById('makeros-theme');if(s&&typeof d.css==='string')s.textContent=d.css;"
            "if(d.lang&&d.lang!==L){L=d.lang;try{localStorage.setItem('selectedLanguage',JSON.stringify(L))}catch(x){}location.reload()}})})();</script>")


def theme_style(c):
    return f'<style id="makeros-theme">{theme_text(c)}</style>'


async def set_ha_language(lang):
    """MakerOS kullanıcısının Home Assistant profil dilini ayarlar (arayüz sunucudaki profil dilini öncelikli kullanır)."""
    c = load_conf()
    ss = list((c.get("mk_sessions") or {}).items())
    if not ss:
        return False
    cid, rt = ss[-1]
    t = await _token(c, {"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid})
    if not t:
        return False
    async with HAWS({**c, "token": t["access_token"]}) as ws:
        await ws.cmd({"type": "frontend/set_user_data", "key": "language",
                      "value": {"language": lang, "number_format": "language", "time_format": "language", "date_format": "language",
                                "time_zone": "local", "first_weekday": "language"}})
    c = load_conf()
    c["ha_lang"] = lang
    save_conf(c)
    return True



# --- MakerOS kullanıcısı: Home Assistant'a otomatik giriş -------------------------
# MakerOS'tan açılan Home Assistant, eklentinin oluşturduğu "MakerOS" kullanıcısıyla (yönetici, yalnız yerel ağ)
# giriş yapmış olarak açılır: entegrasyon/eklenti ekleyebilir. Giriş yalnız MakerOS oturumu açık kullanıcıya
# verilen tek kullanımlık, 60 sn'lik biletle olur; vekile doğrudan gelen HA'nın kendi giriş ekranını görür.

MK_NAME = "MakerOS"
TICKETS: dict = {}  # bilet → son geçerlilik zamanı


def new_ticket():
    now = time.time()
    for k in [k for k, v in TICKETS.items() if v < now]:
        del TICKETS[k]
    t = secrets.token_urlsafe(24)
    TICKETS[t] = now + 60
    return t


def take_ticket(t):
    return bool(t) and TICKETS.pop(t, 0) > time.time()


class HAWS:
    """Yönetici erişim belirteciyle Home Assistant WebSocket API'si (kullanıcı oluşturma vb.)."""

    def __init__(self, c):
        self.c, self.i, self.cm, self.ws = c, 0, None, None

    async def __aenter__(self):
        u = urlsplit(norm_url(self.c.get("url")))
        url = ("wss" if u.scheme == "https" else "ws") + "://" + u.netloc + "/api/websocket"
        try:
            self.cm = SESSION.ws_connect(url, ssl=ssl_ctx(self.c))
            self.ws = await self.cm.__aenter__()
            await self.ws.receive_json(timeout=10)  # auth_required
            await self.ws.send_json({"type": "auth", "access_token": self.c.get("token", "")})
            m = await self.ws.receive_json(timeout=10)
        except (aiohttp.ClientError, asyncio.TimeoutError, TypeError, ValueError) as e:
            raise HAError(m(f"Home Assistant WebSocket'e bağlanılamadı ({e.__class__.__name__}).", f"Cannot connect to the Home Assistant WebSocket ({e.__class__.__name__}).")) from None
        if m.get("type") != "auth_ok":
            raise HAError(m("Erişim belirteci geçersiz.", "Access token is invalid."))
        return self

    async def __aexit__(self, *a):
        if self.cm:
            await self.cm.__aexit__(*a)

    async def cmd(self, msg):
        self.i += 1
        await self.ws.send_json({"id": self.i, **msg})
        while True:
            r = await self.ws.receive_json(timeout=15)
            if r.get("id") == self.i:
                break
        if not r.get("success"):
            err = (r.get("error") or {}).get("message") or m("işlem reddedildi", "operation rejected")
            if (r.get("error") or {}).get("code") == "unauthorized":
                err = m("Bu işlem yönetici belirteci ister (Home Assistant'ta yönetici kullanıcının belirteci).", "This needs an admin token (from an admin user in Home Assistant).")
            raise HAError(f"Home Assistant: {err}")
        return r.get("result")


async def ensure_mk_user(force_new=False):
    """MakerOS kullanıcısını (yönetici, yalnız yerel ağ) bir kez oluşturur; parola yalnız bu cihazda saklanır."""
    c = load_conf()
    u = c.get("mk_user")
    if u and u.get("username") and u.get("password") and not force_new:
        return u
    pw = secrets.token_urlsafe(24)
    async with HAWS(c) as ws:
        users = await ws.cmd({"type": "config/auth/list"}) or []
        if force_new and u and u.get("id"):
            try:  # eski (parolası tutmayan) MakerOS kullanıcısını kaldır
                await ws.cmd({"type": "config/auth/delete", "user_id": u["id"]})
                users = [x for x in users if x.get("id") != u["id"]]
            except HAError:
                pass
        taken = {x.get("username") for x in users}
        uname = "makeros"
        while uname in taken:
            uname = f"makeros-{secrets.token_hex(2)}"
        user = await ws.cmd({"type": "config/auth/create", "name": MK_NAME, "group_ids": ["system-admin"], "local_only": True})
        uid = user["user"]["id"]
        await ws.cmd({"type": "config/auth_provider/homeassistant/create", "user_id": uid, "username": uname, "password": pw})
        try:
            await ws.cmd({"type": "person/create", "name": MK_NAME, "user_id": uid})
        except HAError:
            pass  # kişi zaten var / bileşen kapalı: sorun değil
    u = {"id": uid, "username": uname, "password": pw}
    c = load_conf()
    c["mk_user"], c["mk_sessions"] = u, {}
    save_conf(c)
    print(f"Home Assistant'ta {MK_NAME} kullanıcısı oluşturuldu ({uname}).", flush=True)
    return u


async def _token(c, data):
    async with SESSION.post(norm_url(c["url"]) + "/auth/token", data=data, ssl=ssl_ctx(c)) as r:
        return await r.json(content_type=None) if r.status == 200 else None


async def mk_tokens(client_id):
    """Vekil kökeni (client_id) için MakerOS kullanıcısının oturumu; yenileme belirteci saklanıp yeniden kullanılır."""
    c = load_conf()
    rt = (c.get("mk_sessions") or {}).get(client_id)
    if rt:
        t = await _token(c, {"grant_type": "refresh_token", "refresh_token": rt, "client_id": client_id})
        if t and t.get("access_token"):
            t["refresh_token"] = rt
            return t
    base = norm_url(c["url"])
    for attempt in (0, 1):
        u = await ensure_mk_user(force_new=attempt == 1)
        async with SESSION.post(base + "/auth/login_flow", ssl=ssl_ctx(c),
                                json={"client_id": client_id, "handler": ["homeassistant", None], "redirect_uri": client_id}) as r:
            flow = await r.json(content_type=None)
        if not isinstance(flow, dict) or "flow_id" not in flow:
            raise HAError(m("Home Assistant giriş akışı başlatılamadı.", "Could not start the Home Assistant login flow."))
        async with SESSION.post(f"{base}/auth/login_flow/{flow['flow_id']}", ssl=ssl_ctx(c),
                                json={"client_id": client_id, "username": u["username"], "password": u["password"]}) as r:
            res = await r.json(content_type=None)
        if isinstance(res, dict) and res.get("type") == "create_entry":
            t = await _token(c, {"grant_type": "authorization_code", "code": res["result"], "client_id": client_id})
            if not t:
                raise HAError(m("Home Assistant oturum belirteci vermedi.", "Home Assistant did not issue a session token."))
            c = load_conf()
            ss = c.get("mk_sessions") or {}
            ss[client_id] = t["refresh_token"]
            c["mk_sessions"] = dict(list(ss.items())[-10:])
            save_conf(c)
            return t
        # Kullanıcı silinmiş/parolası değişmiş: bir kez yeniden oluştur.
    raise HAError(m("MakerOS kullanıcısıyla giriş yapılamadı.", "Could not sign in as the MakerOS user."))


async def onboarding_pending(c):
    """Yeni kurulmuş Home Assistant: ilk kullanıcı henüz oluşturulmamış mı?"""
    try:
        async with SESSION.get(norm_url(c["url"]) + "/api/onboarding", ssl=ssl_ctx(c), timeout=aiohttp.ClientTimeout(total=8)) as r:
            steps = await r.json(content_type=None) if r.status == 200 else []
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False
    return any(isinstance(x, dict) and x.get("step") == "user" and not x.get("done") for x in steps or [])


async def onboard_as_makeros(c):
    """Home Assistant'ın ilk kurulumunu MakerOS yapar: sahip hesap doğrudan "MakerOS" olur (başka kullanıcı oluşmaz),
    eklentinin erişim belirteci de bu hesaptan üretilir. Parola yalnız bu cihazda saklanır."""
    base, ctx = norm_url(c["url"]), ssl_ctx(c)
    cid = base + "/"
    pw = secrets.token_urlsafe(24)

    async def post(path, auth=None, **kw):
        h = {"Authorization": f"Bearer {auth}"} if auth else {}
        async with SESSION.post(base + path, ssl=ctx, headers=h, **kw) as r:
            d = await r.json(content_type=None) if r.content_length != 0 else {}
            if r.status >= 400:
                raise HAError(m(f"Home Assistant ilk kurulumu başarısız ({path}: HTTP {r.status}).", f"Home Assistant first-time setup failed ({path}: HTTP {r.status})."))
            return d or {}

    code = (await post("/api/onboarding/users", json={"client_id": cid, "name": MK_NAME, "username": "makeros", "password": pw, "language": "tr"}))["auth_code"]
    tok = await _token(c, {"grant_type": "authorization_code", "code": code, "client_id": cid})
    if not tok:
        raise HAError(m("Home Assistant ilk kurulumu: oturum alınamadı.", "Home Assistant first-time setup: could not get a session."))
    at = tok["access_token"]
    for path, data in (("/api/onboarding/core_config", None), ("/api/onboarding/analytics", None),
                       ("/api/onboarding/integration", {"client_id": cid, "redirect_uri": cid})):
        try:
            await post(path, auth=at, json=data)
        except HAError:
            pass  # adım zaten tamam ya da bu sürümde yok
    c2 = {**c, "token": at}
    async with HAWS(c2) as ws:
        me = await ws.cmd({"type": "auth/current_user"})
        llt = await ws.cmd({"type": "auth/long_lived_access_token", "client_name": "MakerOS", "lifespan": 3650})
    c = load_conf()
    c.update(token=llt, mk_user={"id": me["id"], "username": "makeros", "password": pw, "owner": True}, mk_sessions={})
    save_conf(c)
    print("Home Assistant ilk kurulumu MakerOS hesabıyla yapıldı.", flush=True)


async def configure_core(lang=None):
    """Yerleşik HA'nın sistem ayarları: konum/ülke/saat dilimi HA'nın kendi algılamasıyla (config/core/detect), dil MakerOS'un
    dili, metrik birimler. Konum yoksa harita boş (0,0) açılıyor, sunucu dili "en" iken panel adları İngilizce kalıyordu."""
    c = load_conf()
    lang = lang or c.get("lang") or "tr"
    async with HAWS(c) as ws:
        try:
            det = await ws.cmd({"type": "config/core/detect"}) or {}
        except (HAError, asyncio.TimeoutError):
            det = {}
        upd = {"language": lang, "unit_system": "metric",
               "time_zone": det.get("time_zone") or os.environ.get("TZ") or "Europe/Istanbul",
               "country": det.get("country") or "TR", "currency": det.get("currency") or "TRY"}
        if det.get("latitude") is not None and det.get("longitude") is not None:
            upd.update(latitude=det["latitude"], longitude=det["longitude"])
        elif c.get("core_configured") is None:
            upd.update(latitude=39.0, longitude=35.0)  # Türkiye ortası; algılanamazsa harita en azından ülkede açılır
        await ws.cmd({"type": "config/core/update", **upd})
    c = load_conf()
    c["core_configured"] = lang
    save_conf(c)
    print(f"Home Assistant sistem ayarları: dil {lang}, {upd.get('country')}, {upd.get('time_zone')}", flush=True)


def login_script(t):
    data = {k: t[k] for k in ("access_token", "refresh_token", "expires_in")}
    data["token_type"] = "Bearer"
    # Home Assistant arayüzü oturumu localStorage "hassTokens"tan okur (kendi girişindeki "beni hatırla" ile aynı).
    return ("<script>(function(){var t=" + json.dumps(data) + ";t.hassUrl=location.origin;t.clientId=location.origin+'/';"
            "t.expires=Date.now()+t.expires_in*1000;try{localStorage.setItem('hassTokens',JSON.stringify(t))}catch(e){}"
            "history.replaceState(null,'',location.pathname+location.hash)})();</script>")


# --- ters vekil (Home Assistant arayüzü) ----------------------------------------

def strip_frame_blockers(h):
    h.pop("X-Frame-Options", None)
    csp = h.get("Content-Security-Policy")
    if csp:
        parts = [p for p in csp.split(";") if not p.strip().lower().startswith("frame-ancestors")]
        if any(p.strip() for p in parts):
            h["Content-Security-Policy"] = ";".join(parts)
        else:
            h.pop("Content-Security-Policy", None)


async def proxy(req: web.Request):
    c = load_conf()
    base = norm_url(c.get("url"))
    if not base:
        return web.Response(status=503, text="Home Assistant adresi ayarlanmadı (MakerOS → Home Assistant → Ayarlar).", content_type="text/plain")
    # MakerOS'tan gelen tek kullanımlık bilet: Home Assistant'a iletilmez, sayfaya MakerOS oturumu eklenir.
    ticket = req.query.get("makeros_ticket")
    rel = req.rel_url
    if ticket:
        q = [(k, v) for k, v in rel.query.items() if k != "makeros_ticket"]
        rel = rel.with_query(q or None)
    login = take_ticket(ticket) and c.get("auto_login", True) and req.method == "GET"
    target = base + str(rel)
    hdrs = {k: v for k, v in req.headers.items()
            if k.lower() not in HOP and not k.lower().startswith(("x-forwarded", "sec-websocket")) and k.lower() not in ("forwarded", "origin", "accept-encoding", "cookie")}
    # Yalnız HTML'i değiştiririz; sıkıştırmasız istemek ayrıştırmayı basitleştirir (yerel ağda fark etmez).
    hdrs["Accept-Encoding"] = "identity"

    if req.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_ws(req, base, c)

    try:
        r = await PROXY.request(req.method, target, headers=hdrs, data=req.content if req.body_exists else None,
                                  allow_redirects=False, ssl=ssl_ctx(c), timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return web.Response(status=502, text=f"Home Assistant'a ulaşılamadı: {e.__class__.__name__}", content_type="text/plain")
    try:
        out = {k: v for k, v in r.headers.items() if k.lower() not in HOP and k.lower() != "content-encoding"}
        strip_frame_blockers(out)
        loc = out.get("Location")
        if loc and loc.startswith(base):
            out["Location"] = loc[len(base):] or "/"
        if r.headers.get("Content-Type", "").startswith("text/html"):
            body = await r.read()
            html = body.decode("utf-8", errors="replace")
            css = theme_style(c)
            html = html.replace("</head>", css + "</head>", 1) if "</head>" in html else css + html
            head = theme_head(c)
            if login:
                try:
                    head += login_script(await mk_tokens(f"http://{req.host}/"))
                    if c.get("lang") and load_conf().get("ha_lang") != c.get("lang"):
                        await set_ha_language(c["lang"])
                except HAError as e:
                    print("Otomatik giriş:", e, flush=True)
            mh = re.search(r"<head[^>]*>", html)  # dil ve oturum, Home Assistant betiklerinden önce
            html = html[:mh.end()] + head + html[mh.end():] if mh else head + html
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


async def proxy_ws(req, base, c):
    u = urlsplit(base)
    target = ("wss" if u.scheme == "https" else "ws") + "://" + u.netloc + str(req.rel_url)
    srv = web.WebSocketResponse(autoping=True, max_msg_size=0)
    await srv.prepare(req)
    OPEN_WS.add(srv)
    try:
        async with PROXY.ws_connect(target, ssl=ssl_ctx(c), autoping=True, max_msg_size=0, heartbeat=None) as cli:
            async def pump(a, b):
                async for m in a:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        await b.send_str(m.data)
                    elif m.type == aiohttp.WSMsgType.BINARY:
                        await b.send_bytes(m.data)
                    elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        break
                await b.close()

            await asyncio.gather(pump(srv, cli), pump(cli, srv), return_exceptions=True)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print("WebSocket vekili:", e.__class__.__name__, e, flush=True)
    OPEN_WS.discard(srv)
    if not srv.closed:
        await srv.close()
    return srv


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


# --- MakerOS sayfaları (eklenti kiti: kabuğun tema belirteçleri ve dili, canlı eşitleme) ------------------

GEAR = '<svg width=15 height=15 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.8 stroke-linecap=round stroke-linejoin=round><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx=12 cy=12 r=3 /></svg>'

PAGE = r"""<!doctype html><html lang=tr><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Home Assistant</title>""" + KIT_HEAD + r"""<style>
html,body{height:100%}#ha{position:fixed;inset:var(--tb,0px) 0 0 0;width:100%;height:calc(100% - var(--tb,0px));border:0;background:var(--bg)}
#gear{position:fixed;right:0;top:calc(50% + var(--tb,0px)/2);transform:translateY(-50%);z-index:5;width:22px;height:44px;padding:0 0 0 2px;border-radius:10px 0 0 10px;
 background:color-mix(in srgb,var(--card) 90%,transparent);color:var(--fg);border:1px solid var(--bd2);border-right:0;box-shadow:0 4px 14px rgba(0,0,0,.15);opacity:.45;transition:opacity .2s,width .2s}
#gear:hover{opacity:1;width:30px}#panel{position:fixed;inset:0;z-index:10;background:rgba(0,0,0,.35);display:grid;place-items:center;padding:16px}
.box{background:var(--bg);border:1px solid var(--bd2);border-radius:16px;padding:20px;width:min(640px,100%);max-height:100%;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.3)}
h2{margin:0 0 4px;font-size:17px}h3{margin:18px 0 8px;font-size:13px}input.f{flex:1}
.ents{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:4px 12px;max-height:240px;overflow:auto;border:1px solid var(--bd);border-radius:10px;padding:8px;background:var(--card)}
.ents label{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--fg);overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
#msg{margin-top:10px}.center{position:fixed;inset:0;display:grid;place-items:center;text-align:center;padding:24px}
</style>
<iframe id=ha class=hide allow="fullscreen; clipboard-read; clipboard-write; microphone" referrerpolicy=no-referrer></iframe>
<div id=wait class="center"><div><div id=wt class=sub data-t=loading></div><pre id=wlog class=hide style="max-width:min(720px,90vw);max-height:160px;overflow:auto;background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:8px;font-size:11px;white-space:pre-wrap;text-align:left"></pre></div></div>
<button id=gear data-t-title=settings>""" + GEAR + r"""</button>
<div id=panel class=hide><div class=box>
 <h2>Home Assistant</h2><div class=sub data-t=intro></div>
 <h3 data-t=modeT></h3>
 <div class=row><label><input type=radio name=mode value=local> <span data-t=local></span></label></div>
 <div class=row style="margin-top:6px"><label><input type=radio name=mode value=remote> <span data-t=remote></span></label></div>
 <div id=rem class=hide style="margin-top:8px">
 <div class=row><input class=f id=url data-t-placeholder=urlPh></div>
 <div class=row style="margin-top:8px"><input class=f id=tok type=password></div>
 <div class=row style="margin-top:8px"><label><input type=checkbox id=ins> <span data-t=insecure></span></label></div>
 </div>
 <div class=row style="margin-top:8px"><label><input type=checkbox id=auto> <span data-t=autoLogin></span></label></div>
 <div class=sub style="margin-top:6px" data-t-html=help></div>
 <div id=add class=hide><h3 data-t=addTo></h3><div class=row>
  <button class=g data-go="config/integrations/dashboard" data-t=integration></button>
  <button class=g data-go="config/devices/dashboard" data-t=devices></button>
  <button class=g data-go="config/automation/dashboard" data-t=automations></button>
  <button class="g hide" id=store data-go="hassio/store" data-t=addonStore></button>
  <button class=g data-go="config/users" data-t=users></button></div>
  <div class=sub style="margin-top:6px" id=who></div></div>
 <div id=wsec class=hide><h3 data-t=widget></h3><div class=sub style="margin-bottom:6px" data-t-html=widgetHelp></div>
 <input class=f id=q data-t-placeholder=search style="width:100%;margin-bottom:6px"><div class=ents id=ents></div></div>
 <div class=row style="margin-top:16px;justify-content:flex-end"><span class=err id=msg style="flex:1"></span><button class=g id=close data-t=close></button><button id=save data-t=save></button></div>
</div></div>
<script>
MK.i18n({
 tr:{loading:'Home Assistant yükleniyor…',settings:'Ayarlar',intro:"Bağlantı, MakerOS kullanıcısı ve masaüstü widget'ı.",conn:'Bağlantı',
  urlPh:'Adres (ör. http://homeassistant.local:8123)',tokPh:'Uzun ömürlü erişim belirteci',tokKept:'Belirteç kayıtlı (değiştirmek için yapıştırın)',
  insecure:'Kendinden imzalı HTTPS sertifikasına izin ver',autoLogin:'MakerOS kullanıcısıyla otomatik giriş (yönetici; entegrasyon ve eklenti ekleyebilir)',
  help:"<b>Yeni kurulmuş</b> bir Home Assistant ise belirteç gerekmez: ilk kurulumu MakerOS yapar ve tek kullanıcı <b>MakerOS</b> olur. Zaten kurulmuş Home Assistant için belirteç: Profil → Güvenlik → Uzun ömürlü erişim belirteçleri (yönetici kullanıcıdan); eklenti bununla <b>MakerOS</b> kullanıcısını bir kez oluşturur. Dil ve tema MakerOS'la eşitlenir. Ağdan doğrudan :8795'e gelenler Home Assistant'ın kendi giriş ekranını görür.",
  addTo:"Home Assistant'a ekle",integration:'Entegrasyon ekle',devices:'Cihazlar',automations:'Otomasyonlar',addonStore:'Eklenti mağazası',users:'Kullanıcılar',
  asUser:"Home Assistant'ta {u} kullanıcısı olarak",ownLogin:'Home Assistant girişinizle',noStore:'eklenti mağazası yalnız Home Assistant OS/Supervised kurulumlarda vardır',
  widget:"Masaüstü widget'ı",widgetHelp:"Seçtikleriniz widget'ta kısayol olarak görünür (en çok 12). Widget'ı Eklentiler → Home Assistant → <b>Masaüstüne ekle</b> ile açın.",
  modeT:'Çalışma kipi',local:'Bu cihazda çalıştır (önerilen)',remote:"Ağdaki başka bir Home Assistant'a bağlan",
  pulling:'Home Assistant indiriliyor (ilk açılış, ~600 MB, birkaç dakika)…',starting:'Home Assistant başlatılıyor (ilk açılış birkaç dakika sürebilir)…',onboarding:'Home Assistant MakerOS hesabıyla kuruluyor…',
  error:'Home Assistant çalışmıyor',idle:'Bekleniyor…',
  search:'Ara',close:'Kapat',save:'Kaydet',https:'MakerOS HTTPS ile açıldı; Home Assistant arayüzü (HTTP) bu sayfaya gömülemez. Yeni sekmede açın:'},
 en:{loading:'Loading Home Assistant…',settings:'Settings',intro:'Connection, MakerOS user and desktop widget.',conn:'Connection',
  urlPh:'Address (e.g. http://homeassistant.local:8123)',tokPh:'Long-lived access token',tokKept:'Token saved (paste to change)',
  insecure:'Allow self-signed HTTPS certificate',autoLogin:'Sign in automatically as the MakerOS user (admin; can add integrations and add-ons)',
  help:'For a <b>freshly installed</b> Home Assistant no token is needed: MakerOS does the first-time setup and <b>MakerOS</b> is the only user. For an existing Home Assistant use a token from Profile → Security → Long-lived access tokens (admin user); the plugin then creates the <b>MakerOS</b> user once. Language and theme follow MakerOS. Visitors reaching :8795 directly see Home Assistant\'s own login.',
  addTo:'Add to Home Assistant',integration:'Add integration',devices:'Devices',automations:'Automations',addonStore:'Add-on store',users:'Users',
  asUser:'Signed in to Home Assistant as {u}',ownLogin:'With your Home Assistant login',noStore:'the add-on store exists only on Home Assistant OS/Supervised',
  widget:'Desktop widget',widgetHelp:'Selected entities appear as shortcuts in the widget (up to 12). Turn the widget on in Plugins → Home Assistant → <b>Add to desktop</b>.',
  modeT:'Mode',local:'Run on this device (recommended)',remote:'Connect to another Home Assistant on the network',
  pulling:'Downloading Home Assistant (first start, ~600 MB, a few minutes)…',starting:'Starting Home Assistant (first start can take a few minutes)…',onboarding:'Setting up Home Assistant with the MakerOS account…',
  error:'Home Assistant is not running',idle:'Waiting…',
  search:'Search',close:'Close',save:'Save',https:'MakerOS was opened over HTTPS; the Home Assistant UI (HTTP) cannot be embedded. Open it in a new tab:'}});
// data-t-html (yalnız bu sayfanın kendi sabit metinleri)
function trHtml(){for(const n of document.querySelectorAll('[data-t-html]'))n.innerHTML=MK.t(n.dataset.tHtml)}trHtml();
const $=id=>document.getElementById(id);let CFG={},ALL=[],SEL=new Set();
async function api(p,b){const r=await mkFetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{cache:'no-store'});
 const d=await r.json().catch(()=>({error:'HTTP '+r.status}));if(!r.ok||d.ok===false)throw new Error(d.error||('HTTP '+r.status));return d}
// Masaüstü penceresinde kabuğun başlık şeridi içeriğin üstündedir: Home Assistant şeridin altından başlar.
function embedMode(){try{if(parent===window)return'standalone';const d=parent.document.documentElement;return d.dataset.ui==='web'?'page':d.dataset.layout==='mobile'?'mobile':'window'}catch{return'standalone'}}
function titlebarH(){try{return parseInt(getComputedStyle(parent.document.documentElement).getPropertyValue('--mk-titlebar-height'))||38}catch{return 38}}
if(embedMode()==='window')document.documentElement.style.setProperty('--tb',titlebarH()+'px');
const haOrigin=()=>`http://${location.hostname}:${CFG.proxy_port}`,haURL=()=>haOrigin()+'/';
// Tema/dil: kabuğun gerçek paleti Home Assistant'a; değişince yenilemeden uygulanır (dil değişirse HA yeniden yüklenir).
async function pushTheme(){const th=MK.theme();try{const r=await api('api/theme',{accent:th.accent,dark:th.dark,lang:MK.lang,colors:th.colors});
 const f=$('ha');if(f.src&&f.contentWindow)f.contentWindow.postMessage({mk:'ha-theme',css:r.css,lang:r.lang},haOrigin())}catch{}}
// MakerOS kullanıcısıyla giriş: her açılışta tek kullanımlık bilet (yalnız oturum açmış MakerOS kullanıcısı alabilir).
async function haSrc(path=''){let t=null;try{t=(await api('api/ticket')).ticket}catch{}return haURL()+path+(t?(path.includes('?')?'&':'?')+'makeros_ticket='+encodeURIComponent(t):'')}
function go(path){panel(false);haSrc(path).then(u=>{$('ha').src=u})}
function openHA(){if(location.protocol==='https:'){$('wait').innerHTML='';$('wait').append(Object.assign(document.createElement('div'),{textContent:MK.t('https')}),
 Object.assign(document.createElement('a'),{href:haURL(),target:'_blank',textContent:haURL()}));return}
 const f=$('ha');f.onload=()=>{$('wait').classList.add('hide');f.classList.remove('hide')};haSrc().then(u=>{f.src=u})}
function drawEnts(){const q=$('q').value.toLowerCase();$('ents').innerHTML='';
 for(const e of ALL.filter(e=>!q||e.name.toLowerCase().includes(q)||e.id.includes(q))){const l=document.createElement('label');const c=document.createElement('input');c.type='checkbox';c.checked=SEL.has(e.id);
  c.onchange=()=>{c.checked?SEL.add(e.id):SEL.delete(e.id);if(SEL.size>12){SEL.delete(e.id);c.checked=false}};l.title=e.id;l.append(c,e.name);$('ents').append(l)}}
function drawWho(i){$('who').textContent=(i.auto_login&&i.user?MK.t('asUser',{u:i.user}):MK.t('ownLogin'))+` · Home Assistant ${i.version}`+(i.supervisor?'':' · '+MK.t('noStore'))}
let INFO=null;
async function panel(show){$('panel').classList.toggle('hide',!show);if(!show)return;document.querySelectorAll('[name=mode]').forEach(r=>r.checked=r.value===CFG.mode);
 $('rem').classList.toggle('hide',CFG.mode!=='remote');$('url').value=CFG.mode==='remote'?(CFG.url||''):'';$('ins').checked=!!CFG.insecure;$('auto').checked=CFG.auto_login!==false;
 $('tok').placeholder=MK.t(CFG.has_token?'tokKept':'tokPh');$('msg').textContent='';
 if(CFG.url&&CFG.has_token)api('api/info').then(i=>{INFO=i;$('add').classList.remove('hide');$('store').classList.toggle('hide',!i.supervisor);drawWho(i)}).catch(()=>{});
 if(CFG.url&&CFG.has_token){try{const [s,w]=await Promise.all([api('api/states'),api('api/widget')]);ALL=s.entities;SEL=new Set(w.entities);$('wsec').classList.remove('hide');drawEnts()}catch(e){$('msg').textContent=e.message}}}
$('gear').onclick=()=>panel(true);document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>go(b.dataset.go));$('close').onclick=()=>panel(false);$('q').oninput=drawEnts;
document.querySelectorAll('[name=mode]').forEach(r=>r.onchange=()=>$('rem').classList.toggle('hide',!(r.value==='remote'&&r.checked)));
$('save').onclick=async()=>{$('save').disabled=true;$('msg').textContent='';try{const mode=document.querySelector('[name=mode]:checked')?.value||'local';const b={mode,url:$('url').value,insecure:$('ins').checked,auto_login:$('auto').checked};if($('tok').value)b.token=$('tok').value.trim();
 await api('api/config',b);$('tok').value='';if(!$('wsec').classList.contains('hide'))await api('api/widget',{entities:[...SEL]});
 CFG=await api('api/config');panel(false);OPENED=false;$('ha').removeAttribute('src')}catch(e){$('msg').textContent=e.message}finally{$('save').disabled=false}};
MK.on((_,ch)=>{trHtml();$('tok').placeholder=MK.t(CFG.has_token?'tokKept':'tokPh');if(INFO)drawWho(INFO);pushTheme()});
// Yerleşik kip: HA hazır olana dek ilerleme; hazır olunca arayüz açılır.
let OPENED=false;
async function poll(){let st={};try{st=await api('api/status')}catch(e){st={state:'error',error:e.message}}
 if(CFG.mode==='local'&&st.state!=='running'){$('wait').classList.remove('hide');$('ha').classList.add('hide');OPENED=false;
  $('wt').textContent=MK.t(st.state||'idle')+(st.error?' — '+st.error:'');const lg=$('wlog');lg.textContent=(st.log||[]).slice(-12).join('\n');lg.classList.toggle('hide',!(st.log||[]).length)}
 else if(!OPENED){CFG=await api('api/config');if(CFG.mode==='remote'&&(!CFG.url||!CFG.has_token)){$('wait').classList.add('hide');panel(true)}else{OPENED=true;openHA()}}
 setTimeout(poll,OPENED?15000:3000)}
(async()=>{CFG=await api('api/config');await pushTheme();poll()})();
</script>"""

WIDGET = r"""<!doctype html><html lang=tr><meta charset=utf-8><meta name=viewport content='width=device-width'>""" + KIT_HEAD + r"""<style>
html,body{margin:0;padding:0;background:transparent!important;overflow:hidden}
.c{border-radius:18px;background:color-mix(in srgb,var(--card) 88%,transparent);border:1px solid var(--bd);padding:12px 14px;backdrop-filter:blur(20px)}
.h{display:flex;align-items:center;gap:8px;cursor:pointer}.h b{flex:1;font-size:13px}.x{background:none;border:0;color:var(--mut);padding:2px 6px;font-size:16px;line-height:1}
.g{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.e{border-radius:12px;background:var(--ctl);border:1px solid var(--bd);padding:9px 10px;text-align:left;color:var(--fg);cursor:pointer;min-width:0;font:inherit}
.e .n{font-size:12px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.e .s{font-size:15px;font-weight:600;margin-top:2px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.e.on{background:var(--a);border-color:var(--a);color:var(--on)}.e.ro{cursor:default}.e:disabled{opacity:.6}.m{font-size:12px;color:var(--mut);margin-top:8px}
</style>
<div class=c id=c><div class=h id=hd><svg width=18 height=18 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round style="color:var(--a)"><path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
<b id=t>Home Assistant</b><button class=x id=x data-t-title=remove>×</button></div><div class=g id=g></div><div class=m id=m></div></div>
<script>
MK.i18n({
 tr:{remove:'Masaüstünden kaldır',on:'Açık',off:'Kapalı',open:'Açık',closed:'Kapalı',locked:'Kilitli',unlocked:'Kilitsiz',unavailable:'Yok',unknown:'—',playing:'Oynuyor',paused:'Durdu',idle:'Boşta',
  pick:'Varlık seçmek için Home Assistant → ⚙ Ayarlar.',noConn:'bağlantı yok'},
 en:{remove:'Remove from desktop',on:'On',off:'Off',open:'Open',closed:'Closed',locked:'Locked',unlocked:'Unlocked',unavailable:'Unavailable',unknown:'—',playing:'Playing',paused:'Paused',idle:'Idle',
  pick:'Choose entities in Home Assistant → ⚙ Settings.',noConn:'no connection'}});
const $=id=>document.getElementById(id);let LIST=[];
const post=m=>parent.postMessage(m,location.origin);
const size=()=>post({mk:'widget-size',h:Math.ceil($('c').getBoundingClientRect().height)+2});new ResizeObserver(size).observe($('c'));
$('hd').onclick=e=>{if(e.target.id!=='x')post({mk:'open',app:'makeros.plugin.homeassistant'})};$('x').onclick=()=>post({mk:'widget-close'});
function fmt(e){if(/^\d{4}-\d\d-\d\dT/.test(e.state))return new Date(e.state).toLocaleTimeString(MK.locale,{hour:'2-digit',minute:'2-digit'});
 const k=['on','off','open','closed','locked','unlocked','unavailable','unknown','playing','paused','idle'].includes(e.state)?MK.t(e.state):e.state;return k+(e.unit?' '+e.unit:'')}
function draw(){$('g').innerHTML='';for(const e of LIST){const b=document.createElement('button');const act=e.actions[0];
 b.className='e'+(['on','open','playing','unlocked'].includes(e.state)?' on':'')+(act?'':' ro');b.title=e.id;
 const n=document.createElement('div');n.className='n';n.textContent=e.name;const s=document.createElement('div');s.className='s';s.textContent=fmt(e);b.append(n,s);
 if(act&&act!=='open')b.onclick=async()=>{b.disabled=true;try{const r=await mkFetch('api/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_id:e.id,action:act})});
  const d=await r.json();if(d.entity)Object.assign(e,d.entity);else $('m').textContent=d.error||''}finally{b.disabled=false;draw()}};$('g').append(b)}
 $('m').textContent=LIST.length?'':MK.t('pick')}
async function tick(){try{const r=await mkFetch('api/widget?states=1',{cache:'no-store'});const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.error||MK.t('noConn'));
 if(d.location)$('t').textContent=d.location;LIST=d.states;draw()}catch(e){$('m').textContent=e.message}size()}
MK.on(()=>draw());tick();setInterval(()=>{if(document.visibilityState==='visible')tick()},10000);
</script>"""


# --- MakerOS uç noktaları --------------------------------------------------------

def j(d, status=200):
    return web.json_response(d, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False), headers={"Cache-Control": "no-store"})


@web.middleware
async def errors(req, handler):
    LANG.set("en" if req.headers.get("X-MakerOS-Lang", "").startswith("en") else "tr")
    try:
        return await handler(req)
    except HAError as e:
        return j({"ok": False, "error": str(e)}, 502)
    except (ValueError, json.JSONDecodeError) as e:
        return j({"ok": False, "error": m(f"Geçersiz istek: {e}", f"Invalid request: {e}")}, 400)


async def body(req):
    if (req.content_length or 0) > 64 << 10:
        raise ValueError(m("istek çok büyük", "request too large"))
    d = await req.json()
    if not isinstance(d, dict):
        raise ValueError(m("JSON nesnesi bekleniyor", "JSON object expected"))
    return d


async def h_index(_):
    return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})


async def h_widget_page(_):
    return web.Response(text=WIDGET, content_type="text/html", headers={"Cache-Control": "no-store"})


async def h_health(_):
    c = load_conf()
    out = {"ok": True, "plugin": PLUGIN_ID, "configured": bool(c.get("url") and c.get("token")), "connected": False,
           "proxy": {"port": proxy_port(c), "listening": PROXY_STATE["port"] == proxy_port(c), "error": PROXY_STATE["error"]}}
    if out["configured"]:
        try:
            out["version"] = (await ha("GET", "/config", timeout=5) or {}).get("version", "")
            out["connected"] = True
        except HAError as e:
            out["error"] = str(e)
    return j(out)


async def h_status(_):
    c = load_conf()
    if mode_of(c) != "local":
        return j({"ok": True, "mode": "remote", "state": "running"})
    return j({"ok": True, "mode": "local", **LSTATE})


async def h_config_get(_):
    c = load_conf()
    return j({"mode": mode_of(c), "url": c.get("url", ""), "has_token": bool(c.get("token")), "insecure": bool(c.get("insecure")), "proxy_port": proxy_port(c),
              "auto_login": c.get("auto_login", True)})


async def h_config_post(req):
    b = await body(req)
    c = load_conf()
    mode = b.get("mode", mode_of(c))
    if mode not in ("local", "remote"):
        raise ValueError("mode local|remote")
    old = mode_of(c)
    if mode == "local":
        if isinstance(b.get("auto_login"), bool):
            c["auto_login"] = b["auto_login"]
        if old != "local":
            c.pop("mk_user", None)
            c.pop("mk_sessions", None)
            c.pop("token", None)
            c["url"] = LOCAL_URL
        c["mode"] = "local"
        save_conf(c)
        start_local()
        return j({"ok": True, "mode": "local"})
    c["mode"] = "remote"
    if old == "local":
        await stop_local()
        c.pop("token", None)
    url = norm_url(b.get("url"))
    if not url.startswith(("http://", "https://")) or len(url) > 300:
        return j({"ok": False, "error": m("Adres http:// veya https:// ile başlamalı.", "Address must start with http:// or https://.")}, 400)
    if norm_url(c.get("url")) != url:
        c.pop("mk_user", None)
        c.pop("mk_sessions", None)
    c["url"] = url
    if isinstance(b.get("auto_login"), bool):
        c["auto_login"] = b["auto_login"]
    if isinstance(b.get("token"), str) and b["token"].strip():
        c["token"] = b["token"].strip()
    c["insecure"] = bool(b.get("insecure"))
    if "proxy_port" in b:
        p = b["proxy_port"]
        if not isinstance(p, int) or not 1024 <= p <= 65535 or p == PORT:
            raise ValueError(m("proxy_port 1024–65535 olmalı", "proxy_port must be 1024–65535"))
        c["proxy_port"] = p
    save_conf(c)
    await restart_proxy()
    onboarded = False
    if await onboarding_pending(c):  # yeni HA: kurulumu MakerOS yapar, belirteç gerekmez
        await onboard_as_makeros(c)
        onboarded = True
    elif not c.get("token"):
        return j({"ok": False, "error": m("Bu Home Assistant zaten kurulmuş: yönetici kullanıcıdan bir erişim belirteci girin.", "This Home Assistant is already set up: enter an access token from an admin user.")}, 400)
    msg = (await ha("GET", "/") or {}).get("message", "")  # "API running."
    return j({"ok": True, "message": msg, "onboarded": onboarded})


async def h_states(_):
    return j(await states())


async def h_call(req):
    b = await body(req)
    return j(await call(b.get("entity_id"), b.get("action")))


async def h_widget_get(req):
    c = load_conf()
    ids = widget_ids(c)
    if req.query.get("states") != "1":
        return j({"ok": True, "entities": ids})
    if not ids:  # seçim yoksa ilk yönetilebilen birkaç varlık
        s = await states()
        return j({"ok": True, "location": s["location"], "states": [e for e in s["entities"] if e["actions"]][:6]})
    raw = await ha("GET", "/states") or []
    by = {s.get("entity_id"): s for s in raw if isinstance(s, dict)}
    return j({"ok": True, "states": [entity(by[i]) for i in ids if i in by]})


async def h_widget_post(req):
    b = await body(req)
    ids = b.get("entities")
    if not isinstance(ids, list) or len(ids) > 12 or not all(isinstance(i, str) and ENTITY_RE.match(i) for i in ids):
        raise ValueError(m("entities en çok 12 varlık kimliği olmalı", "entities must be at most 12 entity ids"))
    c = load_conf()
    c["widget_entities"] = ids
    save_conf(c)
    return j({"ok": True})


async def h_ticket(_):
    c = load_conf()
    return j({"ok": True, "ticket": new_ticket() if c.get("auto_login", True) else None})


async def h_info(_):
    cfg = await ha("GET", "/config") or {}
    c = load_conf()
    return j({"ok": True, "version": cfg.get("version", ""), "location": cfg.get("location_name", ""),
              "supervisor": "hassio" in (cfg.get("components") or []), "user": MK_NAME if c.get("mk_user") else None,
              "auto_login": c.get("auto_login", True)})


async def h_theme(req):
    """Kabuğun teması ve dili (eklenti sayfası açılınca ve değişince). Dönüş: Home Assistant'a uygulanacak CSS."""
    b = await body(req)
    c = load_conf()
    acc, dark, lang, pal = b.get("accent"), b.get("dark"), b.get("lang"), b.get("colors")
    new = {"accent": acc if isinstance(acc, str) and HEX_RE.match(acc) else c.get("accent"),
           "dark": dark if isinstance(dark, bool) else None,
           "lang": lang if lang in ("tr", "en") else c.get("lang", "tr"),
           "palette": {k: v for k, v in pal.items() if isinstance(v, str) and k in PALETTE[False] and COLOR_RE.match(v)} if isinstance(pal, dict) else c.get("palette")}
    if any(c.get(k) != v for k, v in new.items()):
        c.update(new)
        save_conf(c)
    if c.get("lang") and c.get("ha_lang") != c["lang"] and c.get("mk_sessions"):
        try:
            await set_ha_language(c["lang"])
        except HAError as e:
            print("Home Assistant dili:", e, flush=True)
    # Yerleşik HA: sunucu dili de (panel adları, bildirimler) MakerOS'u izler.
    if mode_of(c) == "local" and c.get("token") and c.get("core_configured") not in (None, c.get("lang")):
        try:
            await configure_core(c.get("lang"))
        except HAError as e:
            print("Home Assistant sistem dili:", e, flush=True)
    return j({"ok": True, "css": theme_text(c), "lang": c.get("lang", "tr")})


# --- yaşam döngüsü ------------------------------------------------------------

PROXY_STATE = {"runner": None, "port": None, "error": None}
OPEN_WS: set = set()


async def restart_proxy():
    """Vekil dinleyicisini (yeniden) kurar: port değişince ya da ilk ayarda."""
    port = proxy_port()
    if PROXY_STATE["runner"] and PROXY_STATE["port"] == port:
        return
    if PROXY_STATE["runner"]:
        await PROXY_STATE["runner"].cleanup()
        PROXY_STATE.update(runner=None, port=None)
    app = web.Application(client_max_size=64 << 20)
    app.router.add_route("*", "/{tail:.*}", proxy)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    try:
        for i in range(20):
            try:
                await web.TCPSite(runner, host=os.environ.get("MAKEROS_HA_PROXY_HOST", "0.0.0.0"), port=port).start()
                break
            except OSError:
                if i == 19:
                    raise
                await asyncio.sleep(0.5)
        PROXY_STATE.update(runner=runner, port=port, error=None)
        print(f"Home Assistant arayüz vekili: 0.0.0.0:{port}", flush=True)
    except OSError as e:
        await runner.cleanup()
        PROXY_STATE["error"] = f"Vekil portu {port} açılamadı: {e.strerror}"
        print(PROXY_STATE["error"], flush=True)


async def main():
    global SESSION, PROXY
    SESSION = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), headers={"User-Agent": UA})
    PROXY = aiohttp.ClientSession(auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar())
    app = web.Application(middlewares=[errors])
    r = app.router
    r.add_get("/", h_index)
    r.add_get("/widget", h_widget_page)
    r.add_get("/health", h_health)
    r.add_get("/api/config", h_config_get)
    r.add_post("/api/config", h_config_post)
    r.add_get("/api/states", h_states)
    r.add_post("/api/call", h_call)
    r.add_get("/api/widget", h_widget_get)
    r.add_post("/api/widget", h_widget_post)
    r.add_post("/api/theme", h_theme)
    r.add_get("/api/ticket", h_ticket)
    r.add_get("/api/info", h_info)
    r.add_get("/api/status", h_status)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    try:
        # Güncelleme/yeniden başlatmada eski kopya birkaç saniye içinde portu bırakır: bekle.
        for i in range(30):
            try:
                await web.TCPSite(runner, "127.0.0.1", PORT).start()
                break
            except OSError:
                if i == 29:
                    raise
                await asyncio.sleep(0.5)
    except OSError:
        print(f"Home Assistant eklentisi: port {PORT} kullanımda, zaten çalışan kopya kullanılıyor.", flush=True)
        await SESSION.close()
        await PROXY.close()
        return
    print("Home Assistant eklentisi; port", PORT, flush=True)
    await restart_proxy()
    if mode_of(load_conf()) == "local":
        start_local()
    loop = asyncio.get_running_loop()
    watcher = loop.create_task(watch_local())
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, STOPPING.set)
    await STOPPING.wait()
    watcher.cancel()
    await stop_local()
    for w in list(OPEN_WS):  # tarayıcı yeniden bağlanır (yeni kopyaya)
        await w.close()
    if PROXY_STATE["runner"]:
        await PROXY_STATE["runner"].cleanup()
    await runner.cleanup()
    await SESSION.close()
    await PROXY.close()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
