# MakerOS Eklentileri

[MakerOS](https://github.com/ferhatozcelik/maker-os) için topluluk eklentileri. MakerOS'taki **Eklentiler → Mağaza**
sekmesi bu deponun `plugins/` klasörlerini doğrudan listeler; "Kur" klasörü indirir, her dosyayı depodakiyle
(git özeti) karşılaştırır ve kullanıcının hesabıyla kurar. Derleme veya yayın adımı yoktur: klasör `main`'e girdiği an mağazada görünür.

| Eklenti | Açıklama |
|---|---|
| [Konya HackerSpace](plugins/khs) | KHS teması, kapı durumu (açık/kapalı) göstergesi ve konyahackerspace.org uygulaması |
| [Pi-hole](plugins/pihole) | Ağdaki Pi-hole'un (v6 API) istatistikleri, en çok engellenenler, engellemeyi aç/kapat/5 dk kapat |
| [Home Assistant](plugins/homeassistant) | Home Assistant'ın kendi arayüzü MakerOS penceresinde ve temasıyla (vekil, port 8795); seçili varlıklar masaüstü widget'ı olarak |
| [Chromecast](plugins/chromecast) | Ağdaki Google Cast cihazlarını bulma; bağlantı oynatma, duraklat/durdur, ses (pychromecast) |

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
5. Çalıştığını deneyin: `python3 tools/smoke.py <id>` (aşağıya bakın).
6. PR açın.

## Eklentileri başlatıp denemek (duman testi)

`tools/smoke.py` eklentiyi makerd'nin yaptığı gibi `uv run` ile başlatır ve denetler: `/health` yanıtı,
arayüz (`/`), model/hazırlık durumu (`state`), isteğe bağlı gerçek bağlantı (`--set`) ve SIGTERM ile temiz kapanış.

```sh
python3 tools/smoke.py                                   # tüm eklentiler, yerelde (MakerOS gerekmez)
python3 tools/smoke.py pihole --set pihole='{"url":"http://192.168.1.2","password":"..."}'
python3 tools/smoke.py --makeros http://makeros.local --password ...   # gerçek MakerOS'a kurar, başlatır, vekil üzerinden dener
```

Ayarlar (adres, parola, belirteç) eklenti klasöründe değil `~/.config/makeros-plugins/<id>.json` dosyasında tutulur:
dışa aktarılan zip'e girmez, güncellemede silinmez.

Kurallar: zip en fazla 15 MB; simge (`icon`) SVG/PNG/WebP ve en fazla 32 KB; sembolik bağ, `.venv` ve gizli dosyalar pakete girmez.

## Lisans

GPL-3.0-or-later. Her eklenti kendi klasöründe farklı bir lisans belirtebilir.
