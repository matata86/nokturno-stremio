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
# schémata, která umí rozklíčovat `Engine.resolve()`; jiné se k přehrání nepustí
SCHEMATA = ("ws:", "hs:", "st:", "dav:", "streamuj:", "http://", "https://")


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
        if not isinstance(vnitrni, str) or not vnitrni.startswith(SCHEMATA):
            continue
        out.append({"id": f"ws-{index}", "url": odkaz(vnitrni), "lang": "ces"})
    return out


def stream_object(popis, odkaz, jmeno_doplnku="Nokturno"):
    """Jeden stream z `Engine._describe()` do podoby pro Stremio.

    `odkaz(vnitrni_url)` vrátí adresu na tuhle službu — odkazy WebShare platí jen
    chvíli, takže se nesmí vydávat dopředu, ale až když si přehrávač řekne.
    """
    vnitrni = popis.get("url") or ""
    if not vnitrni:
        return None

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

    # vlevo v úzkém sloupci je místo jen na jméno a kvalitu; HDR/DV k ní patří,
    # protože rozhoduje o tom, jestli má smysl sahat po velkém souboru
    vlevo = kvalita + (" " + " ".join(obraz[:1]) if obraz else "")
    objekt = {
        "url": odkaz(vnitrni),
        "name": jmeno_doplnku + (f"\n{vlevo}" if vlevo else ""),
        "description": "\n".join(r for r in radky if r),
        "behaviorHints": {},
    }

    velikost = _velikost_bajtu(popis)
    if velikost:
        objekt["behaviorHints"]["videoSize"] = velikost
    if nazev_souboru:
        objekt["behaviorHints"]["filename"] = nazev_souboru
    if nazev_souboru.lower().endswith(NE_PRO_WEB):
        objekt["behaviorHints"]["notWebReady"] = True
    # aby „další díl“ držel stejný zdroj i kvalitu jako ten, co uživatel pustil
    if kvalita:
        objekt["behaviorHints"]["bingeGroup"] = f"nokturno-{zdroj}-{kvalita}".replace(" ", "-").lower()

    podtitulky = titulky(popis, odkaz)
    if podtitulky:
        objekt["subtitles"] = podtitulky
    return objekt


def streams_response(popisy, odkaz):
    """Celá odpověď endpointu `/stream/…`."""
    out = []
    for popis in popisy:
        objekt = stream_object(popis, odkaz)
        if objekt:
            out.append(objekt)
    return {"streams": out}


def manifest(verze, zdroje=(), nastaveno=True):
    """Manifest doplňku.

    Zatím jen `stream`: katalogy z Cinemety a TMDB má Stremio samo, přidaná
    hodnota Nokturna jsou zdroje streamů.

    **`idPrefixes` se záměrně neuvádí.** S `["tt"]` se Stremio neptalo na tituly
    otevřené z cizích katalogů, které mají vlastní tvar id — třeba SosacTV2 —
    a doplněk u nich mlčel, i když ten film umí najít. Bez omezení se zeptá vždy
    a co neumíme, vrátí prázdno; jeden dotaz navíc je levnější než chybějící
    streamy u poloviny knihovny.
    """
    popis = "Streamy z WebShare, Sosáče, Sledujteto a HellSpy k filmům a seriálům, které už ve Stremiu vidíš."
    if zdroje:
        popis += " Nastavené zdroje: " + ", ".join(zdroje) + "."
    return {
        "id": "community.nokturno",
        "version": verze,
        "name": "Nokturno",
        "description": popis,
        "logo": "https://raw.githubusercontent.com/matata86/plugin.video.nokturno/main/resources/media/icon2.png",
        "resources": ["stream"],
        "types": ["movie", "series"],
        "catalogs": [],
        "behaviorHints": {"configurable": False, "configurationRequired": not nastaveno},
    }


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")
