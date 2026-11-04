#!/usr/bin/env python3
"""MakerOS eklenti kataloğunu derler.

plugins/<id>/ klasörlerinden dist/<id>-<sürüm>.zip ve dist/index.json üretir.
Zip'ler tekrarlanabilirdir (sabit tarih, sıralı dosyalar): aynı içerik aynı
SHA-256'yı verir. makerd index.json'u okur, zip'i indirip özetini denetler ve
kurar.

    python3 tools/build.py [--out dist]
"""

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
VER_RE = re.compile(r"^\d+(\.\d+){0,3}$")
MAX_ZIP = 15 << 20
MAX_ICON = 32 << 10
SKIP_DIRS = {".venv", "__pycache__", ".git"}
ICON_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp"}


def fail(msg):
    sys.exit(f"hata: {msg}")


def files_of(d: Path):
    out = []
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if p.is_symlink():
            fail(f"{p}: sembolik bağ olamaz")
        if p.is_file():
            out.append((rel.as_posix(), p))
    return out


def build_zip(d: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, p in files_of(d):
            zi = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, p.read_bytes())
    return buf.getvalue()


def icon_uri(d: Path, rel: str):
    if not rel:
        return ""
    p = (d / rel).resolve()
    ct = ICON_TYPES.get(p.suffix.lower())
    if not ct or not p.is_file() or d.resolve() not in p.parents or p.stat().st_size > MAX_ICON:
        return ""
    return f"data:{ct};base64," + base64.b64encode(p.read_bytes()).decode()


def entry(d: Path, out: Path):
    try:
        m = json.loads((d / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        fail(f"{d.name}/plugin.json okunamadı: {e}")
    pid, ver = m.get("id", ""), m.get("version", "")
    if pid != d.name or not ID_RE.match(pid):
        fail(f"{d.name}: id klasör adıyla aynı olmalı (küçük harf, rakam, tire; en fazla 40)")
    if not VER_RE.match(ver):
        fail(f"{pid}: version noktalı sayı olmalı (ör. 1.2.0)")
    if not m.get("name") or len(m.get("description", "")) > 500:
        fail(f"{pid}: name gerekli, description en fazla 500 karakter")
    ep = m.get("entrypoint") or "main.py"
    for need in (ep, "pyproject.toml"):
        if not (d / need).is_file():
            fail(f"{pid}: {need} yok")
    data = build_zip(d)
    if len(data) > MAX_ZIP:
        fail(f"{pid}: zip 15 MB'ı aşıyor")
    name = f"{pid}-{ver}.zip"
    (out / name).write_bytes(data)
    tags = [t for t, k in (("theme", "theme"), ("status", "status"), ("widget", "widget")) if m.get(k)]
    if m.get("web_port"):
        tags.append("web")
    return {
        "id": pid,
        "name": m["name"],
        "version": ver,
        "description": m.get("description", ""),
        "author": m.get("author", ""),
        "icon": icon_uri(d, m.get("icon", "")),
        "tags": tags,
        "zip": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    out = Path(ap.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.zip"):
        old.unlink()
    dirs = sorted(p.parent for p in (ROOT / "plugins").glob("*/plugin.json"))
    plugins = sorted((entry(d, out) for d in dirs), key=lambda e: e["name"].lower())
    idx = {"format": 1, "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "plugins": plugins}
    (out / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for e in plugins:
        print(f"{e['id']:<24} {e['version']:<10} {e['size']:>9} B  {e['sha256'][:12]}")
    print(f"{len(plugins)} eklenti → {out}/index.json")


if __name__ == "__main__":
    main()
