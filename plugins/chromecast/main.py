"""Chromecast eklentisi: ağdaki Google Cast cihazlarını bulur ve oynatmayı yönetir (pychromecast).

Bulma mDNS (zeroconf) ile yapılır; mDNS'in geçmediği ağlarda cihaz IP'leri Ayarlar'dan elle eklenebilir.
Ayarlar ~/.config/makeros-plugins/chromecast.json dosyasındadır.

  GET  /                        → arayüz
  GET  /health                  → {"ok": true, "discovery", "devices"}
  GET  /api/devices             → [{uuid, name, model, host, type, connected, app, volume, muted, media}]
  POST /api/rescan              ← {"hosts"?: ["192.168.1.40", ...]}   (bulmayı yeniden başlatır)
  POST /api/devices/<uuid>/<op> ← op: connect | play {url, content_type?, title?} | pause | resume | stop
                                     | quit | volume {level 0–1} | mute {muted} | seek {position sn}
"""
import errno
import ipaddress
import json
import mimetypes
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pychromecast
import zeroconf
from pychromecast.discovery import CastBrowser, SimpleCastListener

try:
    from importlib.metadata import version as _v

    PYCC_VERSION = _v("pychromecast")
except Exception:
    PYCC_VERSION = ""

PLUGIN_ID = os.environ.get("MAKEROS_PLUGIN_ID", "chromecast")
PORT = int(os.environ.get("MAKEROS_PLUGIN_PORT", "8793"))
CONF = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "makeros-plugins" / f"{PLUGIN_ID}.json"
UUID_RE = re.compile(r"^[0-9a-f-]{32,36}$")


_req = threading.local()  # istek başına dil


def m(tr, en):
    """Metin MakerOS dilinde (sayfa X-MakerOS-Lang başlığını gönderir)."""
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
    tmp.write_text(json.dumps(c), encoding="utf-8")
    os.replace(tmp, CONF)


class Casts:
    """Arka planda sürekli bulma; bağlantılar (Chromecast nesneleri) istek üzerine kurulur ve saklanır."""

    def __init__(self):
        self.lock = threading.RLock()
        self.zconf = None
        self.browser = None
        self.conns = {}  # uuid(str) → Chromecast
        self.started = 0.0

    def start(self):
        with self.lock:
            self.stop()
            hosts = [h for h in load_conf().get("hosts", []) if isinstance(h, str)]
            self.zconf = zeroconf.Zeroconf()
            self.browser = CastBrowser(SimpleCastListener(), self.zconf, hosts or None)
            self.browser.start_discovery()
            self.started = time.time()
            print(f"Bulma başladı (elle eklenen: {hosts or 'yok'})", flush=True)

    def stop(self):
        with self.lock:
            for c in self.conns.values():
                try:
                    c.disconnect(timeout=2)
                except Exception:
                    pass
            self.conns.clear()
            if self.browser:
                try:
                    self.browser.stop_discovery()
                except Exception:
                    pass
            if self.zconf:
                try:
                    self.zconf.close()
                except Exception:
                    pass
            self.browser = self.zconf = None

    def infos(self):
        with self.lock:
            return dict(self.browser.devices) if self.browser else {}

    def connect(self, uuid):
        with self.lock:
            c = self.conns.get(uuid)
            if c is not None and c.socket_client.is_connected:
                return c
            info = next((i for u, i in self.infos().items() if str(u) == uuid), None)
            if info is None:
                raise LookupError(m("Cihaz bulunamadı (ağda mı? Yeniden ara'yı deneyin).", "Device not found (is it on the network? Try Rescan)."))
            c = pychromecast.get_chromecast_from_cast_info(info, self.zconf, tries=2, retry_wait=1, timeout=8)
        c.wait(timeout=10)
        with self.lock:
            self.conns[uuid] = c
        return c

    def describe(self, uuid, info):
        c = self.conns.get(uuid)
        d = {
            "uuid": uuid,
            "name": info.friendly_name,
            "model": info.model_name,
            "manufacturer": info.manufacturer or "",
            "type": info.cast_type,  # cast | audio | group
            "host": info.host,
            "port": info.port,
            "connected": bool(c and c.socket_client.is_connected),
        }
        if d["connected"]:
            st = c.status
            d.update(app=(st.display_name if st else "") or "", volume=round(st.volume_level, 2) if st else None, muted=bool(st and st.volume_muted))
            ms = c.media_controller.status
            if ms and ms.player_state not in (None, "UNKNOWN"):
                d["media"] = {
                    "state": ms.player_state,  # PLAYING | PAUSED | BUFFERING | IDLE
                    "title": ms.title or "",
                    "url": ms.content_id or "",
                    "position": round(ms.adjusted_current_time or 0),
                    "duration": round(ms.duration) if ms.duration else None,
                }
        return d

    def list(self):
        out = [self.describe(str(u), i) for u, i in self.infos().items()]
        out.sort(key=lambda d: (d["type"] == "group", d["name"].lower()))
        return out


CASTS = Casts()


def guess_type(url, given):
    if given:
        if not re.match(r"^[a-z]+/[a-z0-9.+-]+$", given):
            raise ValueError(m("content_type geçersiz (ör. video/mp4)", "invalid content_type (e.g. video/mp4)"))
        return given
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "application/x-mpegURL"
    if path.endswith(".mpd"):
        return "application/dash+xml"
    return mimetypes.guess_type(path)[0] or "video/mp4"


def op(uuid, name, b):
    c = CASTS.connect(uuid)
    mc = c.media_controller
    if name == "connect":
        pass
    elif name == "play":
        url = b.get("url", "")
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc or len(url) > 2000:
            raise ValueError(m("Adres http:// veya https:// ile başlamalı", "Address must start with http:// or https://"))
        host = u.hostname or ""
        if host == "localhost" or host.startswith("127.") or host == "::1":
            raise ValueError(m(f"{host} Chromecast'ten erişilemez; bilgisayarın ağ adresini kullanın", f"{host} is not reachable from the Chromecast; use the computer's network address"))
        mc.play_media(url, guess_type(url, b.get("content_type")), title=str(b.get("title") or "")[:200] or None)
        mc.block_until_active(timeout=15)
    elif name == "pause":
        mc.pause()
    elif name == "resume":
        mc.play()
    elif name == "stop":
        mc.stop()
    elif name == "quit":
        c.quit_app()
    elif name == "volume":
        lv = b.get("level")
        if not isinstance(lv, (int, float)) or not 0 <= lv <= 1:
            raise ValueError(m("level 0 ile 1 arasında olmalı", "level must be between 0 and 1"))
        c.set_volume(float(lv))
    elif name == "mute":
        c.set_volume_muted(bool(b.get("muted")))
    elif name == "seek":
        pos = b.get("position")
        if not isinstance(pos, (int, float)) or pos < 0:
            raise ValueError(m("position saniye olmalı", "position must be in seconds"))
        mc.seek(float(pos))
    else:
        raise ValueError(m(f"bilinmeyen işlem: {name}", f"unknown operation: {name}"))
    time.sleep(0.4)  # durum güncellemesi gelsin
    info = CASTS.infos()
    inf = next((i for u, i in info.items() if str(u) == uuid), None)
    return {"ok": True, "device": CASTS.describe(uuid, inf) if inf else None}


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
<title>Chromecast</title>""" + KIT_HEAD + r"""<style>
body{padding:20px}.card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:14px 16px;margin-top:12px}
input.f{flex:1}.n{font-weight:600;font-size:15px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--mut);margin-right:6px}
.c .dot{background:var(--ok)}#err{margin-top:8px}input[type=range]{flex:1;padding:0;accent-color:var(--a)}.m{margin-top:8px}.m b{font-weight:600}
</style>
<div class=row style="justify-content:space-between"><div><h1>Chromecast</h1><div class=sub id=info></div></div>
<div class=row><button class=g id=gear data-t=addManual></button><button id=scan data-t=rescan></button></div></div>
<div class="card hide" id=cfg><div class=row><input class=f id=hosts data-t-placeholder=hostsPh><button id=save data-t=saveScan></button></div></div>
<div class=err id=err></div><div id=list></div>
<script>
MK.i18n({
 tr:{addManual:'Elle ekle',rescan:'Yeniden ara',hostsPh:"Cihaz IP'leri, virgülle (mDNS çalışmıyorsa) ör. 192.168.1.40",saveScan:'Kaydet ve ara',
  searching:'Ağdaki Cast cihazları aranıyor…',count:'{n} cihaz',none:'Cihaz bulunamadı. Aynı ağda mı? mDNS engelliyse "Elle ekle".',
  connect:'Bağlan',group:'grup',audio:'ses',app:'Uygulama: {a}',idle:'Boşta',pause:'Duraklat',resume:'Sürdür',stop:'Durdur',quit:'Uygulamayı kapat',
  mute:'Sessiz',unmute:'Sesi aç',vol:'Ses',urlPh:'Oynatılacak bağlantı (mp4, mp3, jpg, m3u8…)',play:'Oynat',
  PLAYING:'Oynatılıyor',PAUSED:'Duraklatıldı',BUFFERING:'Yükleniyor',IDLE:'Boşta'},
 en:{addManual:'Add manually',rescan:'Rescan',hostsPh:'Device IPs, comma separated (if mDNS fails) e.g. 192.168.1.40',saveScan:'Save and scan',
  searching:'Looking for Cast devices on the network…',count:'{n} devices',none:'No devices found. Same network? If mDNS is blocked, use "Add manually".',
  connect:'Connect',group:'group',audio:'audio',app:'App: {a}',idle:'Idle',pause:'Pause',resume:'Resume',stop:'Stop',quit:'Quit app',
  mute:'Mute',unmute:'Unmute',vol:'Volume',urlPh:'Link to play (mp4, mp3, jpg, m3u8…)',play:'Play',
  PLAYING:'Playing',PAUSED:'Paused',BUFFERING:'Buffering',IDLE:'Idle'}});
const $=id=>document.getElementById(id);const T=s=>{s=Math.max(0,s|0);return `${(s/60|0)}:${String(s%60).padStart(2,'0')}`};let busy=false,DEV=[],AGE=0;
async function api(p,b){const r=await mkFetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{cache:'no-store'});
 const d=await r.json().catch(()=>({error:'HTTP '+r.status}));if(!r.ok||d.ok===false)throw new Error(d.error||('HTTP '+r.status));return d}
async function act(d,o,b,btn){busy=true;if(btn)btn.disabled=true;$('err').textContent='';try{await api(`api/devices/${d.uuid}/${o}`,b||{})}catch(e){$('err').textContent=d.name+': '+e.message}finally{busy=false;if(btn)btn.disabled=false;load()}}
function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function card(d){const c=el('div','card'+(d.connected?' c':''));
 const top=el('div','row');top.style.justifyContent='space-between';const l=el('div');const n=el('div','n');n.append(el('span','dot'),d.name);
 l.append(n,el('div','sub',`${d.model||''}${d.type==='group'?' · '+MK.t('group'):d.type==='audio'?' · '+MK.t('audio'):''} · ${d.host}`));top.append(l);
 if(!d.connected){const b=el('button','g',MK.t('connect'));b.onclick=()=>act(d,'connect',{},b);top.append(b)}c.append(top);if(!d.connected)return c;
 const m=el('div','m');if(d.media){m.append(el('b',null,MK.t(d.media.state)),` · ${d.media.title||d.media.url}`);if(d.media.duration)m.append(` · ${T(d.media.position)} / ${T(d.media.duration)}`)}
 else m.append(el('span','sub',d.app?MK.t('app',{a:d.app}):MK.t('idle')));c.append(m);
 const ctl=el('div','row');ctl.style.marginTop='10px';const mk=(t,o,cls,b)=>{const x=el('button',cls,MK.t(t));x.onclick=()=>act(d,o,b,x);ctl.append(x)};
 if(d.media&&d.media.state==='PLAYING')mk('pause','pause','g');if(d.media&&d.media.state==='PAUSED')mk('resume','resume','');
 if(d.media)mk('stop','stop','g');if(d.app)mk('quit','quit','g');mk(d.muted?'unmute':'mute','mute','g',{muted:!d.muted});
 const v=el('input');v.type='range';v.min=0;v.max=100;v.value=Math.round((d.volume||0)*100);v.title=MK.t('vol');v.onchange=()=>act(d,'volume',{level:v.value/100});
 ctl.append(el('span','sub',MK.t('vol')),v);c.append(ctl);
 const p=el('div','row');p.style.marginTop='10px';const u=el('input','f');u.placeholder=MK.t('urlPh');const go=el('button',null,MK.t('play'));
 go.onclick=()=>{if(u.value.trim())act(d,'play',{url:u.value.trim()},go)};u.onkeydown=e=>{if(e.key==='Enter')go.click()};p.append(u,go);c.append(p);return c}
function draw(){$('list').innerHTML='';for(const x of DEV)$('list').append(card(x));
 $('info').textContent=DEV.length?MK.t('count',{n:DEV.length}):(AGE<12?MK.t('searching'):MK.t('none'))}
async function load(){if(busy||document.activeElement?.tagName==='INPUT')return;try{const d=await api('api/devices');DEV=d.devices;AGE=d.age;draw()}catch(e){$('err').textContent=e.message}}
$('gear').onclick=()=>$('cfg').classList.toggle('hide');
$('scan').onclick=async()=>{$('scan').disabled=true;try{await api('api/rescan',{})}catch(e){$('err').textContent=e.message}$('scan').disabled=false;load()};
$('save').onclick=async()=>{const hosts=$('hosts').value.split(/[\s,]+/).filter(Boolean);try{await api('api/rescan',{hosts});$('cfg').classList.add('hide')}catch(e){$('err').textContent=e.message}load()};
MK.on((_,ch)=>{if(ch.has('lang'))draw()});
$('info').textContent=MK.t('searching');api('api/config').then(c=>{$('hosts').value=(c.hosts||[]).join(', ')}).catch(()=>{});load();setInterval(()=>{if(document.visibilityState==='visible')load()},4000);
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
            raise ValueError(m("istek çok büyük", "request too large"))
        d = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(d, dict):
            raise ValueError(m("JSON nesnesi bekleniyor", "JSON object expected"))
        return d

    def _path(self):
        _req.lang = "en" if (self.headers.get("X-MakerOS-Lang") or "").startswith("en") else "tr"
        return "/" + self.path.split("?")[0].strip("/")

    def do_GET(self):
        p = self._path()
        if p == "/health":
            return self._send(200, {"ok": True, "plugin": PLUGIN_ID, "discovery": CASTS.browser is not None,
                                    "devices": len(CASTS.infos()), "pychromecast": PYCC_VERSION})
        if p == "/api/devices":
            return self._send(200, {"ok": True, "devices": CASTS.list(), "age": round(time.time() - CASTS.started)})
        if p == "/api/config":
            return self._send(200, {"hosts": load_conf().get("hosts", [])})
        if p == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        return self._send(404, {"ok": False, "error": m("bulunamadı", "not found")})

    def do_POST(self):
        p = self._path()
        try:
            b = self._body()
            if p == "/api/rescan":
                if "hosts" in b:
                    hosts = b["hosts"]
                    if not isinstance(hosts, list) or len(hosts) > 32:
                        raise ValueError(m("hosts en fazla 32 adreslik liste olmalı", "hosts must be a list of at most 32 addresses"))
                    for h in hosts:
                        ipaddress.ip_address(h)  # yalnız IP (ValueError → 400)
                    c = load_conf()
                    c["hosts"] = hosts
                    save_conf(c)
                CASTS.start()
                return self._send(200, {"ok": True})
            parts = p.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "devices"] and UUID_RE.match(parts[2]):
                return self._send(200, op(parts[2], parts[3], b))
            return self._send(404, {"ok": False, "error": m("bulunamadı", "not found")})
        except LookupError as e:
            return self._send(404, {"ok": False, "error": str(e)})
        except ValueError as e:
            return self._send(400, {"ok": False, "error": m(f"Geçersiz istek: {e}", f"Invalid request: {e}")})
        except Exception as e:  # pychromecast: bağlantı/zaman aşımı
            return self._send(502, {"ok": False, "error": m(f"Cihaz yanıt vermedi ({e.__class__.__name__}: {e})", f"Device did not respond ({e.__class__.__name__}: {e})")})


if __name__ == "__main__":
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        print(f"Chromecast eklentisi: port {PORT} kullanımda, zaten çalışan kopya kullanılıyor.", flush=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    CASTS.start()
    print("Chromecast eklentisi; port", PORT, flush=True)
    try:
        srv.serve_forever()
    finally:
        CASTS.stop()
