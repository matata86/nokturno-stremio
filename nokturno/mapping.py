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

# Stremio čeká jazyk v ISO 639-2, jádro drží dvouznakové kódy
JAZYKY = {"CZ": "ces", "SK": "slk", "EN": "eng", "DE": "deu", "PL": "pol", "HU": "hun", "FR": "fra"}
# kontejnery, které webový přehrávač Stremia nepřehraje — ať to rovnou ví
NE_PRO_WEB = (".mkv", ".avi", ".ts", ".m2ts", ".wmv", ".flv")
# schémata, která umí rozklíčovat `Engine.resolve()`; jiné se k přehrání nepustí
SCHEMATA = ("ws:", "hs:", "streamuj:", "http://", "https://")


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


def _jazyky_s_kanaly(popis):
    """„CZ 5.1“, „EN“ — jazyky zvuku s počtem kanálů, když je znám."""
    kanaly = popis.get("channels") or {}
    out = []
    for kod in popis.get("langs") or []:
        pocet = kanaly.get(kod)
        out.append(f"{kod} {pocet:g}" if isinstance(pocet, (int, float)) else kod)
    return out


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

    podrobnosti = [zdroj]
    jazyky = _jazyky_s_kanaly(popis)
    if jazyky:
        podrobnosti.append("zvuk " + " ".join(jazyky))
    if popis.get("subs"):
        podrobnosti.append("tit. " + " ".join(popis["subs"]))
    if popis.get("size_gb"):
        podrobnosti.append(f"{popis['size_gb']:.1f} GB")
    if popis.get("bitrate"):
        znak = "~" if popis.get("bitrate_est") else ""
        podrobnosti.append(f"{znak}{popis['bitrate']:g} Mb/s")

    objekt = {
        "url": odkaz(vnitrni),
        # vlevo v úzkém sloupci: jméno doplňku a kvalita, nic víc se tam nevejde
        "name": f"{jmeno_doplnku}\n{kvalita}" if kvalita else jmeno_doplnku,
        "description": "\n".join(p for p in (nazev_souboru, "  ·  ".join(podrobnosti)) if p),
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
    hodnota Nokturna jsou zdroje streamů. Proto taky `idPrefixes: ["tt"]` —
    doplněk se chytá na titulech identifikovaných přes IMDb, tedy na všem,
    co Stremio běžně ukazuje.
    """
    popis = "Streamy z WebShare, Sosáče a HellSpy k filmům a seriálům, které už ve Stremiu vidíš."
    if zdroje:
        popis += " Nastavené zdroje: " + ", ".join(zdroje) + "."
    return {
        "id": "community.nokturno",
        "version": verze,
        "name": "Nokturno",
        "description": popis,
        "logo": "https://raw.githubusercontent.com/matata86/plugin.video.nokturno/main/resources/icon.png",
        "resources": ["stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": [],
        "behaviorHints": {"configurable": False, "configurationRequired": not nastaveno},
    }


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")
