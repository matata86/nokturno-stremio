"""Nastavení doplňku — účty zdrojů a předvolby streamů.

Jádro bere nastavení jako obyčejný slovník (`Engine(options, storage_dir)`),
takže tenhle modul jen sbírá hodnoty z prostředí a překládá je na klíče, které
engine čte. Názvy klíčů drží `core/lib/const.py`, ne tento soubor.

Ve Stremiu se nastavení doplňku nosí zakódované v cestě URL, takže každý uživatel
má vlastní adresu. To přijde ve fázi 4 (`/c/<konfigurace>/manifest.json`) a bude
volat `from_mapping()`. Do té doby má instance jednu konfiguraci ze svého
prostředí, což pro domácí server stačí.

Účty se do URL vejdou v otevřené podobě, proto doplněk nikdy nepatří na veřejnou
adresu — viz `pristupy.md` projektu.
"""
import os

from .core.lib.const import LANGS, SORT_ORDERS

# proměnná prostředí → klíč nastavení, který čte engine
PROSTREDI = {
    "NOKTURNO_WS_USERNAME": "ws_username",
    "NOKTURNO_WS_PASSWORD": "ws_password",
    "NOKTURNO_STREAMUJ_USERNAME": "streamuj_username",
    "NOKTURNO_STREAMUJ_PASSWORD": "streamuj_password",
    "NOKTURNO_LUNA_URL": "luna_url",
    "NOKTURNO_LUNA_TOKEN": "luna_token",
    "NOKTURNO_TMDB_API_KEY": "tmdb_api_key",
    "NOKTURNO_HS_ENABLED": "hs_enabled",
    "NOKTURNO_PREF_LANG": "pref_lang",
    "NOKTURNO_PREF_SURROUND": "pref_surround",
    "NOKTURNO_HIDE_SD": "hide_sd",
    "NOKTURNO_MAX_BITRATE": "max_bitrate_mbps",
    "NOKTURNO_SORT": "sort_streams",
}
PRAVDA = ("1", "true", "yes", "ano", "on")
# klíče, u kterých engine čeká pravdivostní hodnotu, ne řetězec
LOGICKE = ("hs_enabled", "pref_surround", "hide_sd")

VYCHOZI = {
    "sort_streams": "quality",   # ve Stremiu je vidět jen několik prvních řádků
    "pref_lang": "CZ",
    "hs_enabled": True,          # HellSpy nepotřebuje účet, není co nastavovat
}


def _cislo(hodnota):
    try:
        return float(str(hodnota).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def from_mapping(raw):
    """Slovník surových hodnot → nastavení pro `Engine`.

    Nezná prostředí ani URL, jen převádí typy a zahazuje nesmysly, takže ho umí
    použít i configure stránka z fáze 4.
    """
    options = dict(VYCHOZI)
    for key, value in (raw or {}).items():
        if value is None or key not in set(PROSTREDI.values()):
            continue
        if key in LOGICKE:
            options[key] = str(value).strip().lower() in PRAVDA if isinstance(value, str) else bool(value)
        elif key == "max_bitrate_mbps":
            options[key] = _cislo(value)
        else:
            options[key] = str(value).strip()

    # nepovolená hodnota by v jádru propadla na výchozí, ale tiše — lepší ji srovnat tady
    if options.get("pref_lang") not in LANGS:
        options["pref_lang"] = ""
    if options.get("sort_streams") not in SORT_ORDERS:
        options["sort_streams"] = VYCHOZI["sort_streams"]
    return options


def from_environ(environ=None):
    """Nastavení z proměnných prostředí `NOKTURNO_*`."""
    env = environ if environ is not None else os.environ
    return from_mapping({klic: env[promenna] for promenna, klic in PROSTREDI.items() if promenna in env})


def sources_summary(engine):
    """Které zdroje jsou nastavené — do logu při startu a do manifestu.

    WebShare se hlásí podle vyplněných údajů, ne podle přihlášení; to je síťové
    volání a tohle se čte při startu i při každém dotazu na manifest.
    """
    zdroje = engine.sources()
    nazvy = {"luna": "Luna", "sosac": "Sosáč", "webshare": "WebShare",
             "hellspy": "HellSpy", "torrent": "torrenty"}
    return [nazvy[k] for k, zapnuto in zdroje.items() if zapnuto and k in nazvy]
