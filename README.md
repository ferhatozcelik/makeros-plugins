# MakerOS Eklentileri

[MakerOS](https://github.com/ferhatozcelik/maker-os) için topluluk eklentileri. MakerOS'taki **Eklentiler → Mağaza**
sekmesi bu deponun kataloğunu listeler; "Kur" eklentiyi indirir, SHA-256 özetini denetler ve kullanıcının hesabıyla kurar.

| Eklenti | Açıklama |
|---|---|
| [Konya HackerSpace](plugins/khs) | KHS teması, kapı durumu (açık/kapalı) göstergesi ve konyahackerspace.org uygulaması |

## Nasıl çalışır

- Her eklenti `plugins/<id>/` klasöründedir: `plugin.json`, `pyproject.toml`, `main.py` (biçim: MakerOS'taki "Şablon").
- `main`'e her gönderimde GitHub Actions `tools/build.py` ile `<id>-<sürüm>.zip` ve `index.json` üretir ve
  [`catalog`](https://github.com/ferhatozcelik/makeros-plugins/releases/tag/catalog) sürümüne yükler.
- makerd `https://github.com/ferhatozcelik/makeros-plugins/releases/download/catalog/index.json` adresini okur
  (`--plugin-catalog` ile değiştirilebilir). Katalogdaki sürüm kurulu olandan yeniyse "Güncelle" düğmesi çıkar.

## Eklenti eklemek

1. MakerOS'ta **Eklentiler → Şablon** ile şablonu indirin ya da `mos plugin new <id>` çalıştırın.
2. `plugins/<id>/` altına koyun. `id` klasör adıyla aynı olmalı (küçük harf, rakam, tire; en fazla 40).
3. `plugin.json` içindeki `version`'ı her değişiklikte artırın (ör. `1.0.0` → `1.0.1`).
4. Yerelde deneyin: `python3 tools/build.py` (ya da `docker run --rm -v "$PWD":/r -w /r python:3-alpine python3 tools/build.py`).
5. PR açın.

Kurallar: zip en fazla 15 MB; simge (`icon`) SVG/PNG/WebP ve en fazla 32 KB; sembolik bağ, `.venv` ve gizli dosyalar pakete girmez.

## Lisans

GPL-3.0-or-later. Her eklenti kendi klasöründe farklı bir lisans belirtebilir.
