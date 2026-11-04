# MakerOS Eklentileri

[MakerOS](https://github.com/ferhatozcelik/maker-os) için topluluk eklentileri. MakerOS'taki **Eklentiler → Mağaza**
sekmesi bu deponun `plugins/` klasörlerini doğrudan listeler; "Kur" klasörü indirir, her dosyayı depodakiyle
(git özeti) karşılaştırır ve kullanıcının hesabıyla kurar. Derleme veya yayın adımı yoktur: klasör `main`'e girdiği an mağazada görünür.

| Eklenti | Açıklama |
|---|---|
| [Konya HackerSpace](plugins/khs) | KHS teması, kapı durumu (açık/kapalı) göstergesi ve konyahackerspace.org uygulaması |

## Nasıl çalışır

- Her eklenti `plugins/<id>/` klasöründedir: `plugin.json`, `pyproject.toml`, `main.py` (biçim: MakerOS'taki "Şablon").
- makerd `main` dalının son commit'indeki ağacı okur (GitHub API, 10 dk önbellek) ve dosyaları o commit'ten indirir.
  Kaynak `--plugin-catalog sahip/depo[@dal]` ile değiştirilebilir.
- `plugin.json`'daki `version` kurulu sürümden büyükse mağazada "Güncelle" düğmesi çıkar.
- GitHub Actions her PR'da `tools/build.py` ile eklentileri denetler.

## Eklenti eklemek

1. MakerOS'ta **Eklentiler → Şablon** ile şablonu indirin ya da `mos plugin new <id>` çalıştırın.
2. `plugins/<id>/` altına koyun. `id` klasör adıyla aynı olmalı (küçük harf, rakam, tire; en fazla 40).
3. `plugin.json` içindeki `version`'ı her değişiklikte artırın (ör. `1.0.0` → `1.0.1`).
4. Denetleyin: `python3 tools/build.py` (ya da `docker run --rm -v "$PWD":/r -w /r python:3-alpine python3 tools/build.py`).
   `dist/<id>-<sürüm>.zip`'i MakerOS'ta **Eklentiler → İçe aktar** ile kurup deneyebilirsiniz.
5. PR açın.

Kurallar: zip en fazla 15 MB; simge (`icon`) SVG/PNG/WebP ve en fazla 32 KB; sembolik bağ, `.venv` ve gizli dosyalar pakete girmez.

## Lisans

GPL-3.0-or-later. Her eklenti kendi klasöründe farklı bir lisans belirtebilir.
