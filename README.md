# Nokturno pro Stremio

Doplněk, který k filmům a seriálům ve Stremiu dohledá streamy z **WebShare**,
**Sosáče** a **HellSpy**. Stejné zdroje jako [doplněk pro Kodi](https://github.com/matata86/plugin.video.nokturno)
a [integrace pro Home Assistant](https://github.com/matata86/nokturno-ha), protože všichni tři
stojí na společném jádru [nokturno-core](https://github.com/matata86/nokturno-core).

> **Jen do domácí sítě.** Stremio nosí nastavení doplňku zakódované v adrese, takže
> účty k WebShare a Streamuj putují v každém požadavku v otevřené podobě.
> Na veřejnou adresu vystavuj jen přes Tailscale Funnel: požadavek z Funnelu
> pozná služba podle hlavičky a bez vlastního nastavení v adrese mu nedá účty
> z prostředí (od 0.2.4). Jiný tunel nebo reverzní proxy tu značku nenese,
> takže by instance pouštěla ven účty z `.env`.

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
| Popisy a katalogy | ne, a nechystají se — ve Stremiu je dodává katalogový doplněk |

Streamy se řadí podle kvality a preferovaného jazyka, protože ve Stremiu je vidět
jen několik prvních řádků. Kvalitu, velikost, bitrate, jazyky zvuku i titulky
odhaduje jádro a co neví ze zdroje, přiznaně označí vlnkou (`~Full HD`).

## Spuštění

```bash
cp .env.example .env      # vyplň účty
docker compose up -d
```

Pak otevřít `http://<adresa stroje>:7127/configure`, vyplnit účty a kliknout na
**Přidat do Stremia**.

Bez Dockeru to jde taky, závislosti žádné nejsou:

```bash
python3 -m nokturno.server --port 7127
```

## Účty jsou v adrese, ne na serveru

Stremio nemá soubor nastavení. Účty se nosí **zakódované v adrese doplňku**, takže
každý, kdo si ho přidá, má vlastní a hledá pod sebou. Server si nic nepamatuje.

```
http://<stroj>:7127/c/<nastavení>/manifest.json
```

Tu adresu vyrobí formulář na `/configure`. Uschovej si ji — bez ní se ke svému
nastavení nedostaneš a vyrobíš si prostě novou.

> Kódování **není šifra**. Kdo adresu má, stahuje z tvého WebShare. Nikomu ji
> neposílej.

Jde i nastavení z prostředí, pak má celá instance jednu konfiguraci a adresa je
bez prefixu. Takhle běžela verze 0.1.0 a funguje to dál.

Hodnoty jsou stejné jako v doplňku pro Kodi, takže se dají opsat z jeho
`settings.xml`.

| Proměnná | Co to je |
|---|---|
| `NOKTURNO_WS_USERNAME`, `NOKTURNO_WS_PASSWORD` | WebShare; místo hesla jde vložit i 40znakový salted hash |
| `NOKTURNO_STREAMUJ_USERNAME`, `NOKTURNO_STREAMUJ_PASSWORD` | Streamuj, kvůli Sosáči; místo hesla i hotový `md5(md5(heslo))` |
| ~~`NOKTURNO_LUNA_URL`, `NOKTURNO_LUNA_TOKEN`~~ | od 0.2.5 se nečtou — Luna má vlastní doplněk do Stremia |
| `NOKTURNO_ST_EMAIL`, `NOKTURNO_ST_PASSWORD` | Sledujteto — hledání chce účet, přehrávání Premium |
| `NOKTURNO_HS_ENABLED` | HellSpy je veřejný, stačí přepínač; zapnutý ve výchozím stavu |
| `NOKTURNO_PREF_LANG`, `NOKTURNO_SORT`, `NOKTURNO_HIDE_SD` | předvolby řazení a filtrování |

Žádný zdroj není povinný. Bez nastavení běží doplněk jen s HellSpy.

## Jak to funguje

```
Stremio ──▶ /stream/movie/tt0133093.json ──▶ Engine.streams() ──▶ WebShare, Sosáč, HellSpy
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

- **Katalog Sosáče** jako volitelný zdroj metadat, zvážit.
- **Méně falešných shod.** Fulltext HellSpy občas vrátí titul, který název jen
  obsahuje, například gameplay videa místo filmu.

## Licence

MIT
