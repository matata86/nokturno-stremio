# Nokturno pro Stremio

[![Ko-fi](https://img.shields.io/badge/Ko--fi-podpo%C5%99%20autora-ff5e5b?logo=ko-fi&logoColor=white)](https://ko-fi.com/matata86) [![PayPal](https://img.shields.io/badge/PayPal-paypal.me%2Fmatata86-00457C?logo=paypal&logoColor=white)](https://paypal.me/matata86) [![Bitcoin](https://img.shields.io/badge/Bitcoin-BTC-f7931a?logo=bitcoin&logoColor=white)](#podpora)

Doplněk, který k filmům a seriálům ve Stremiu (i v Nuviu a dalších klientech
s doplňky Stremia) dohledá streamy z **WebShare**, **Sosáče**, **Sledujteto**
a **HellSpy**. Podrobný návod je ve [wiki](https://github.com/matata86/nokturno-stremio/wiki). Stejné zdroje jako [doplněk pro Kodi](https://github.com/matata86/plugin.video.nokturno)
a [integrace pro Home Assistant](https://github.com/matata86/nokturno-ha), protože všichni tři
stojí na společném jádru [nokturno-core](https://github.com/matata86/nokturno-core).

> **Účty jsou v adrese doplňku.** Stremio nosí nastavení zakódované v adrese, takže
> účty putují v každém požadavku v otevřené podobě — adresu nikomu neposílej.
> Na veřejnou adresu vystavuj jen přes Tailscale Funnel: požadavek z Funnelu
> pozná služba podle hlavičky a bez vlastního nastavení v adrese mu nedá účty
> z prostředí (od 0.2.4). Jiný tunel nebo reverzní proxy tu značku nenese,
> takže by instance pouštěla ven účty z `.env`.
>
> Luna se od 0.2.5 nepoužívá — má vlastní doplněk do Stremia a její odkazy vedou
> do domácí sítě.

## Co umí

Zatím jen streamy, a to je záměr. Katalogy a metadata ve Stremiu už máš z Cinemety,
takže přidaná hodnota Nokturna jsou zdroje. Doplněk se proto chytá na všem, co má
identifikátor IMDb, a k tomu přihodí své streamy.

| | |
|---|---|
| Filmy | ano |
| Seriály | ano, včetně jednotlivých dílů |
| Titulky | ano, z WebShare a Sledujteto |
| Zvuk | jazyk, kanály a kodek — z hlavičky souboru, u Sledujteto přímo z API |
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
**Přidat do Stremia** nebo **Přidat do Nuvia**. Do Streamletu se adresa vkládá ručně
(*Zkopírovat adresu*). Na `/` je úvodní stránka s rozcestníkem všech repozitářů Nokturna.

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
| `NOKTURNO_STATS` | `0` vypne anonymní statistiky, viz níže |

Žádný zdroj není povinný. Bez nastavení běží doplněk jen s HellSpy.

## Jak to funguje

```
Stremio ──▶ /stream/movie/tt0133093.json ──▶ Engine.streams() ──▶ WebShare, Sosáč, HellSpy, Sledujteto
                        ▼
            streamy s odkazem na /play/<payload>
                        ▼
Přehrávač ─▶ /play/<payload> ──▶ Engine.resolve() ──▶ 302 na soubor
```

**Proč to obchází přes `/play/`.** Odkazy WebShare, HellSpy a Sledujteto nesou podpis a platí
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

## Anonymní statistiky

Doplněk posílá anonymní statistiky na stejný sběrný bod jako Nokturno pro Kodi
a Home Assistant: náhodný identifikátor nastavení, verzi, které zdroje jsou
zapnuté a u kterých titulů se otevřely streamy — nejvýš jednou za 6 hodin.
Jedna „instalace" je jedno nastavení doplňku (vlastní adresa), ne celý server.
Účty ani adresa doplňku se neposílají. Vypnutí: `NOKTURNO_STATS=0`.

## Testy

```bash
python3 -m unittest discover -s tests -v
```

Nesahají na síť a nepotřebují účty. Jádro má vlastní testy ve svém repu.

## Stav a co dál

V provozu na vlastní instanci, veřejně přes Tailscale Funnel. Nastavení
s návody je na `/configure`, včetně ověření účtů WebShare a Sledujteto.

Chystá se:

- **Katalog Sosáče** jako volitelný zdroj metadat, zvážit.
- **Méně falešných shod.** Fulltext HellSpy občas vrátí titul, který název jen
  obsahuje, například gameplay videa místo filmu.

## Licence

MIT

---

## Podpora

[![Podpoř Nokturno — Ko-fi, PayPal, Bitcoin](.github/podpora.png)](https://ko-fi.com/matata86)

- **Ko-fi:** https://ko-fi.com/matata86
- **PayPal:** https://paypal.me/matata86
- **Bitcoin:** `bc1qhjwt8xxmuym0xsd50yfpvjph00386uz73gqwlc`
