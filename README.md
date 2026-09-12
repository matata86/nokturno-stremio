# Nokturno pro Stremio

Doplněk, který k filmům a seriálům ve Stremiu dohledá streamy z **WebShare**,
**Sosáče** a **HellSpy**. Stejné zdroje jako [doplněk pro Kodi](https://github.com/matata86/plugin.video.nokturno)
a [integrace pro Home Assistant](https://github.com/matata86/nokturno-ha), protože všichni tři
stojí na společném jádru [nokturno-core](https://github.com/matata86/nokturno-core).

> **Jen do domácí sítě.** Stremio nosí nastavení doplňku zakódované v adrese, takže
> účty k WebShare a Streamuj putují v každém požadavku v otevřené podobě.
> Nevystavuj službu na veřejnou adresu ani přes tunel.

## Co umí

Zatím jen streamy, a to je záměr. Katalogy a metadata ve Stremiu už máš z Cinemety,
takže přidaná hodnota Nokturna jsou zdroje. Doplněk se proto chytá na všem, co má
identifikátor IMDb, a k tomu přihodí své streamy.

| | |
|---|---|
| Filmy | ano |
| Seriály | ano, včetně jednotlivých dílů |
| Titulky | ano, dohledané na WebShare |
| Katalogy | ne, a nechystají se — Stremio je má samo |
| Torrenty | ne, zatím jen v integraci pro Home Assistant |

Streamy se řadí podle kvality a preferovaného jazyka, protože ve Stremiu je vidět
jen několik prvních řádků. Kvalitu, velikost, bitrate, jazyky zvuku i titulky
odhaduje jádro a co neví ze zdroje, přiznaně označí vlnkou (`~Full HD`).

## Spuštění

```bash
cp .env.example .env      # vyplň účty
docker compose up -d
```

Pak v Stremiu **Doplňky → Add addon** a vložit:

```
http://<adresa stroje>:7127/manifest.json
```

Bez Dockeru to jde taky, závislosti žádné nejsou:

```bash
export NOKTURNO_WS_USERNAME=… NOKTURNO_WS_PASSWORD=…
python3 -m nokturno.server --port 7127
```

Adresa `/` vypíše, co je nastavené a jakou adresu vložit do Stremia.

## Účty

Hodnoty jsou stejné jako v doplňku pro Kodi, takže se dají opsat z jeho
`settings.xml`.

| Proměnná | Co to je |
|---|---|
| `NOKTURNO_WS_USERNAME`, `NOKTURNO_WS_PASSWORD` | WebShare; místo hesla jde vložit i 40znakový salted hash |
| `NOKTURNO_STREAMUJ_USERNAME`, `NOKTURNO_STREAMUJ_PASSWORD` | Streamuj, kvůli Sosáči; místo hesla i hotový `md5(md5(heslo))` |
| `NOKTURNO_LUNA_URL`, `NOKTURNO_LUNA_TOKEN` | Luna v domácí síti, nepovinné |
| `NOKTURNO_TMDB_API_KEY` | vlastní klíč TMDB zdarma, kvůli českým názvům bez Luny |
| `NOKTURNO_HS_ENABLED` | HellSpy je veřejný, stačí přepínač; zapnutý ve výchozím stavu |
| `NOKTURNO_PREF_LANG`, `NOKTURNO_SORT`, `NOKTURNO_HIDE_SD` | předvolby řazení a filtrování |

Žádný zdroj není povinný. Bez nastavení běží doplněk jen s HellSpy.

## Jak to funguje

```
Stremio ──▶ /stream/movie/tt0133093.json ──▶ Engine.streams() ──▶ WebShare, Sosáč, HellSpy, Luna
                        ▼
            streamy s odkazem na /play/<payload>
                        ▼
Přehrávač ─▶ /play/<payload> ──▶ Engine.resolve() ──▶ 302 na soubor
```

**Proč to obchází přes `/play/`.** Odkazy WebShare a HellSpy nesou podpis a platí
jen chvíli. Kdyby se vydaly rovnou v odpovědi, do chvíle, než si uživatel stream
vybere, by vyhasly. Endpoint `/play/` proto soubor rozklíčuje až ve chvíli, kdy se
na něj přehrávač skutečně obrátí. Přijímá jen odkazy se známým schématem, jinak by
z něj šlo udělat otevřené přesměrování.

### Proč bez závislostí

Jádro je čistý Python bez vazby na hostitele a jeho volání jsou blokující. HTTP
vrstva proto stojí na `http.server` ze standardní knihovny; `ThreadingHTTPServer`
obslouží každý požadavek ve vlákně, takže dlouhé hledání na WebShare nezablokuje
ostatní dotazy. Nasazení je tím jen zkopírování zdrojáků, bez `pip install`.

### Jádro se needituje tady

`nokturno/core/` je **vysypaná kopie** z repa `nokturno-core`. Oprava udělaná tady
se při příštím rozeslání přepíše. Patří do jádra:

```bash
cd ../../nokturno-core
python3 tools/sync_core.py --check --diff stremio
python3 tools/sync_core.py stremio
```

## Testy

```bash
python3 -m unittest discover -s tests -v
```

Nesahají na síť a nepotřebují účty. Jádro má vlastní testy ve svém repu.

## Stav a co dál

Funkční, nasazené zatím nikde. Ověřeno na skutečných datech: film i díl seriálu
vrátí streamy ze všech tří zdrojů a `/play/` z nich udělá živý odkaz.

Chystá se:

- **Configure stránka** — dnes se nastavuje prostředím, takže instance má jednu
  konfiguraci. Stremio umí nést nastavení v adrese, což dá každému uživateli vlastní.
- **Katalog Sosáče** jako volitelný zdroj metadat, zvážit.
- **Méně falešných shod.** Fulltext HellSpy občas vrátí titul, který název jen
  obsahuje, například gameplay videa místo filmu.

## Licence

MIT
