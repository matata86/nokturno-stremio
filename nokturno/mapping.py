"""Převod mezi jádrem a protokolem Stremia.

Jádro vrací streamy z `Engine._describe()` — hotový popis s kvalitou, velikostí,
bitratem, jazyky a titulky. Tenhle modul z toho skládá objekty, které Stremio
umí vykreslit, a nic nepočítá znovu.

Stremio ukazuje u každého streamu dva texty: `name` v úzkém sloupci vlevo
a `description` vpravo. Vlevo proto patří jen zdroj a kvalita, vpravo název
souboru a podrobnosti.
"""
import base64
import json
import re

# Stremio čeká jazyk v ISO 639-2, jádro drží dvouznakové kódy
JAZYKY = {"CZ": "ces", "SK": "slk", "EN": "eng", "DE": "deu", "PL": "pol", "HU": "hun", "FR": "fra"}

# Vlaječky u streamů jsou ve Stremiu zavedená konvence — jazyk je z nich poznat
# rychleji než z kódu. Jádro slučuje GB/US/UK do EN, proto jen jedna vlajka pro
# angličtinu. Co tady není, se vypíše kódem, ať nezmizí.
VLAJKY = {
    "CZ": "🇨🇿", "SK": "🇸🇰", "EN": "🇬🇧", "DE": "🇩🇪", "PL": "🇵🇱", "HU": "🇭🇺",
    "FR": "🇫🇷", "ES": "🇪🇸", "IT": "🇮🇹", "RU": "🇷🇺", "UA": "🇺🇦", "JP": "🇯🇵",
    "KR": "🇰🇷", "DK": "🇩🇰", "NL": "🇳🇱", "NO": "🇳🇴", "SE": "🇸🇪", "FI": "🇫🇮",
    "PT": "🇵🇹", "TR": "🇹🇷", "RO": "🇷🇴", "BG": "🇧🇬", "GR": "🇬🇷",
}

# značky obrazu a zvuku, které jádro nezná — leží jen v názvu souboru
OBRAZ = (
    (re.compile(r"\bdolby[ ._-]?vision\b|\bdo?vi\b|\bdv\b(?![a-z])", re.I), "DV"),
    (re.compile(r"\bhdr10\+|\bhdr10plus\b", re.I), "HDR10+"),
    (re.compile(r"\bhdr\b", re.I), "HDR"),
    (re.compile(r"\bremux\b", re.I), "REMUX"),
)
ZVUK = (
    (re.compile(r"\batmos\b", re.I), "Atmos"),
    (re.compile(r"\bdts[ ._-]?hd\b|\bdtshd\b", re.I), "DTS-HD"),
    (re.compile(r"\btrue[ ._-]?hd\b", re.I), "TrueHD"),
    (re.compile(r"\bdts[ ._-]?x\b", re.I), "DTS:X"),
)
# kontejnery, které webový přehrávač Stremia nepřehraje — ať to rovnou ví
NE_PRO_WEB = (".mkv", ".avi", ".ts", ".m2ts", ".wmv", ".flv")
# schémata, která umí rozklíčovat `Engine.resolve()`; jiné se k přehrání nepustí.
# Hotové http(s) odkazy (titulky Sledujteto) tu záměrně nejsou: `resolve()` je vrací
# beze změny, takže by `/play/` byl veřejný přesměrovávač kamkoli — vydávají se rovnou.
SCHEMATA = ("ws:", "hs:", "st:", "fs:", "dav:", "streamuj:", "pt:")
PRIME = ("http://", "https://")
# Zdroje, které chtějí u každého požadavku autentizační hlavičku (FastShare cookie
# z přihlášení, vlastní úložiště Basic auth). Vydávají se jako přímá adresa zdroje
# s `behaviorHints.proxyHeaders` — hlavičky pošle přehrávač Stremia sám a data tečou
# rovnou ze zdroje ke klientovi, ne přes tenhle server.
PRES_HLAVICKY = ("dav:", "fs:")
# `proxyHeaders` platí jen u streamu označeného `notWebReady` a webový přehrávač
# takový stream odmítne rovnou (`stremio-video/src/HTMLVideo.js`, `canPlayStream()`).
# Poznat prohlížeč na serveru nejde (viz `routes.klient_z_useragent`), proto se to
# píše rovnou do popisu streamu.
JEN_V_APLIKACI = "⚠️ Ve webovém přehrávači se nepřehraje — jen v aplikaci"


def odkaz_streamu(vnitrni, odkaz):
    """Adresa pro přehrávač: vnitřní odkaz přes `/play/`, hotový http(s) odkaz beze změny."""
    return vnitrni if vnitrni.startswith(PRIME) else odkaz(vnitrni)


def zakoduj(vnitrni_url):
    """Vnitřní odkaz (`ws:<ident>`, `hs:<id>:<hash>`, …) do cesty URL."""
    return base64.urlsafe_b64encode(vnitrni_url.encode("utf-8")).decode("ascii").rstrip("=")


def dekoduj(payload):
    """Zpátky na vnitřní odkaz. Vrací None, když to není odkaz, který umíme přehrát.

    Kontrola schématu není kosmetika: bez ní by `resolve()` neznámou hodnotu
    vrátil nezměněnou a endpoint `/play/` by přesměroval kamkoli.
    """
    try:
        doplneni = "=" * (-len(payload) % 4)
        url = base64.urlsafe_b64decode(payload + doplneni).decode("utf-8")
    except Exception:  # noqa: BLE001 – cokoli nerozluštitelného je prostě neplatné
        return None
    return url if url.startswith(SCHEMATA) else None


def _vlajka(kod):
    return VLAJKY.get(kod, kod)


def _kanaly(pocet):
    if isinstance(pocet, (int, float)) and not isinstance(pocet, bool):
        return f"{pocet:g}"
    return str(pocet).strip() if isinstance(pocet, str) else ""


def _jazyky_s_kanaly(popis):
    """„🇨🇿 5.1 AC3“, „🇬🇧“ — vlaječky zvuku s počtem kanálů a kodekem, když jsou známé.

    Kanály z hlavičky souboru chodí jako text („5.1“), z názvu souboru jako číslo —
    dřív se ukazovalo jen číslo, takže u ověřených stop kanály chyběly. Stopy bez
    rozpoznaného jazyka (Sledujteto ho u stopy neříká) se připíšou jen kanály a kodekem.
    """
    kanaly = popis.get("channels") or {}
    stopy = popis.get("audio") or []
    kodeky = {}
    for stopa in stopy:
        if stopa.get("lang") and stopa.get("codec"):
            kodeky.setdefault(stopa["lang"], stopa["codec"])
    out = []
    for kod in popis.get("langs") or []:
        casti = [_vlajka(kod), _kanaly(kanaly.get(kod)), kodeky.get(kod, "")]
        out.append(" ".join(c for c in casti if c))
    for stopa in stopy:
        if stopa.get("lang"):
            continue
        text = " ".join(c for c in (_kanaly(stopa.get("channels")), stopa.get("codec") or "") if c)
        if text and text not in out:
            out.append(text)
    return out


def _znacky(nazev_souboru, vzory):
    """Značky z názvu souboru — jádro je nezná, protože je nehlásí žádný zdroj."""
    return [znacka for vzor, znacka in vzory if vzor.search(nazev_souboru or "")]


def _delka(popis):
    minut = popis.get("length_min") or 0
    if not minut:
        return ""
    znak = "~" if popis.get("length_est") else ""
    return f"{znak}{minut // 60}:{minut % 60:02d}" if minut >= 60 else f"{znak}{minut} min"


def _velikost_bajtu(popis):
    gb = popis.get("size_gb") or 0
    return int(gb * 1000 ** 3) if gb else None


def titulky(popis, odkaz):
    """Titulky streamu pro Stremio.

    Jádro je dává jako vnitřní odkazy (`ws:<ident>`), takže musí projít stejným
    přesměrováním jako video. Jazyk z nich poznat nejde — WebShare o souboru nic
    neříká — proto se hlásí jako české: fulltext je hledal podle českého názvu.
    """
    out = []
    for index, vnitrni in enumerate(popis.get("subtitles") or []):
        if not isinstance(vnitrni, str) or not vnitrni.startswith(SCHEMATA + PRIME):
            continue
        out.append({"id": f"ws-{index}", "url": odkaz_streamu(vnitrni, odkaz), "lang": "ces"})
    return out


def stream_object(popis, odkaz, jmeno_doplnku="Nokturno", primy=None):
    """Jeden stream z `Engine._describe()` do podoby pro Stremio.

    `odkaz(vnitrni_url)` vrátí adresu na tuhle službu — odkazy WebShare platí jen
    chvíli, takže se nesmí vydávat dopředu, ale až když si přehrávač řekne.

    `primy(vnitrni_url)` vrátí `(adresa, hlavičky)` u zdrojů z `PRES_HLAVICKY`,
    nebo None, když je teď přehrát nejde (vypršelý účet, nedostatek kreditu) —
    takový stream se nenabídne vůbec, protože bez hlaviček by stejně neodehrál.
    """
    vnitrni = popis.get("url") or ""
    if not vnitrni:
        return None

    hlavicky = None
    if vnitrni.startswith(PRES_HLAVICKY):
        primo = primy(vnitrni) if primy else None
        if not primo:
            return None
        adresa, hlavicky = primo

    kvalita = popis.get("quality") or ""
    zdroj = popis.get("source") or ""
    nazev_souboru = popis.get("file") or ""
    obraz = _znacky(nazev_souboru, OBRAZ)
    zvuk_navic = _znacky(nazev_souboru, ZVUK)

    # řádek jazyků: vlaječky zvuku, za nimi titulky
    jazyky = _jazyky_s_kanaly(popis)
    radek_jazyku = []
    if jazyky:
        radek_jazyku.append("🔊 " + "  ".join(jazyky))
    if zvuk_navic:
        radek_jazyku.append(" ".join(zvuk_navic))
    if popis.get("subs"):
        radek_jazyku.append("💬 " + " ".join(_vlajka(k) for k in popis["subs"]))

    # řádek technických údajů
    radek_udaju = []
    if popis.get("size_gb"):
        radek_udaju.append(f"💾 {popis['size_gb']:.1f} GB")
    if popis.get("bitrate"):
        znak = "~" if popis.get("bitrate_est") else ""
        radek_udaju.append(f"⚡ {znak}{popis['bitrate']:g} Mb/s")
    delka = _delka(popis)
    if delka:
        radek_udaju.append(f"⏱ {delka}")
    if zdroj:
        radek_udaju.append(f"🌐 {zdroj}")

    radky = [nazev_souboru, "  ".join(radek_jazyku), "  ".join(radek_udaju)]
    if hlavicky:
        radky.append(JEN_V_APLIKACI)

    # vlevo v úzkém sloupci je místo jen na jméno a kvalitu; HDR/DV k ní patří,
    # protože rozhoduje o tom, jestli má smysl sahat po velkém souboru
    vlevo = kvalita + (" " + " ".join(obraz[:1]) if obraz else "")
    objekt = {
        "url": adresa if hlavicky else odkaz_streamu(vnitrni, odkaz),
        "name": jmeno_doplnku + (f"\n{vlevo}" if vlevo else ""),
        "description": "\n".join(r for r in radky if r),
        "behaviorHints": {},
    }

    velikost = _velikost_bajtu(popis)
    if velikost:
        objekt["behaviorHints"]["videoSize"] = velikost
    if nazev_souboru:
        objekt["behaviorHints"]["filename"] = nazev_souboru
    if nazev_souboru.lower().endswith(NE_PRO_WEB) or hlavicky:
        objekt["behaviorHints"]["notWebReady"] = True
    if hlavicky:
        # bez `notWebReady` Stremio `proxyHeaders` ignoruje (viz addon SDK)
        objekt["behaviorHints"]["proxyHeaders"] = {"request": dict(hlavicky)}
    # aby „další díl“ držel stejný zdroj i kvalitu jako ten, co uživatel pustil
    if kvalita:
        objekt["behaviorHints"]["bingeGroup"] = f"nokturno-{zdroj}-{kvalita}".replace(" ", "-").lower()

    podtitulky = titulky(popis, odkaz)
    if podtitulky:
        objekt["subtitles"] = podtitulky
    return objekt


def streams_response(popisy, odkaz, primy=None):
    """Celá odpověď endpointu `/stream/…`."""
    out = []
    for popis in popisy:
        objekt = stream_object(popis, odkaz, primy=primy)
        if objekt:
            out.append(objekt)
    return {"streams": out}


def upozorneni_nova_adresa(nova_adresa):
    """První položka v seznamu streamů u adresy bez identity: co má uživatel udělat."""
    text = "Klikni na Nastavení doplňku. Pak tento doplněk odeber a přidej nový."
    return {"name": "⚠️ Nokturno", "title": text, "description": text, "externalUrl": nova_adresa}


def upozorneni_blokace(text, odkaz):
    """První (jediná) položka místo streamů, když je adresa doplňku zablokovaná nebo přetížená."""
    return {"name": "⛔ Nokturno", "title": text, "description": text, "externalUrl": odkaz}


def zprava_z_dashboardu(text, odkaz):
    """Řádek s oznámením z dashboardu (obrazovka Zprávy) na začátku seznamu streamů.
    Stremio zahodí stream bez `url`/`infoHash`/`externalUrl`, proto `externalUrl` na úvodní
    stránku doplňku — klik jen otevře prohlížeč, přehrávat se nic nebude."""
    return {"name": "📢 Nokturno", "title": text, "description": text, "externalUrl": odkaz}


def manifest_version(verze):
    """Ořízne betasufix (např. '5.2.1b1' -> '5.2.1') pro pole 'version' manifestu.

    Oficiální Stremio klient parsuje `version` jako čistý semver `x.y.z` a na
    písmeno za patch verzí (beta sufix `b1`, `~beta1`) spadne s chybou
    "unexpected character after patch version number" — Nuvio je benevolentnější,
    proto tam instalace procházela, ale v samotném Stremiu ne.
    """
    shoda = re.match(r"\d+\.\d+\.\d+", verze)
    return shoda.group(0) if shoda else verze


def manifest(verze, zdroje=(), nastaveno=True, katalogy=(), nova_adresa=None):
    """Manifest doplňku.

    Vždy `stream`; `catalog` jen když si uživatel ve formuláři zapnul některý
    z katalogů (`katalogy.Katalogy.manifest`). Bez nich zůstává manifest stejný
    jako dřív, takže stávajícím uživatelům se ve Stremiu nic nezmění.

    **`idPrefixes` se záměrně neuvádí.** S `["tt"]` se Stremio neptalo na tituly
    otevřené z cizích katalogů, které mají vlastní tvar id — třeba SosacTV2 —
    a doplněk u nich mlčel, i když ten film umí najít. Bez omezení se zeptá vždy
    a co neumíme, vrátí prázdno; jeden dotaz navíc je levnější než chybějící
    streamy u poloviny knihovny.
    """
    popis = ("Streamy z WebShare, Sosáče, Sledujteto, FastShare, Přehraj.to a HellSpy "
             "k filmům a seriálům, které už ve Stremiu vidíš.")
    if zdroje:
        popis += " Nastavené zdroje: " + ", ".join(zdroje) + "."
    if nova_adresa:
        popis += " ⚠️ Klikni na Nastavení, tím se vytvoří nová adresa doplňku. Pak tento doplněk odeber a přidej nový."
    return {
        "id": "community.nokturno",
        "version": manifest_version(verze),
        "name": "Nokturno",
        "description": popis,
        "logo": "https://raw.githubusercontent.com/matata86/plugin.video.nokturno/main/resources/media/icon2.png",
        "resources": ["stream", "catalog"] if katalogy else ["stream"],
        "types": ["movie", "series"],
        "catalogs": list(katalogy),
        "behaviorHints": {"configurable": False, "configurationRequired": not nastaveno},
    }


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def json_do_scriptu(data):
    """JSON pro vložení do `<script>` v HTML.

    `json.dumps` neescapuje `</script>` — jméno účtu z adresy by tak ukončilo
    skript a zbytek by prohlížeč spustil jako cizí kód (stránka sbírá hesla).
    Escapované znaky jsou v JSON i JavaScriptu pořád tentýž řetězec.
    """
    return (json_bytes(data).decode("utf-8")
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
