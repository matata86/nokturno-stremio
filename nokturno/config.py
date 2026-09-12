"""Nastavení doplňku — účty zdrojů a předvolby streamů.

Jádro bere nastavení jako obyčejný slovník (`Engine(options, storage_dir)`),
takže tenhle modul jen sbírá hodnoty z prostředí a překládá je na klíče, které
engine čte. Názvy klíčů drží `core/lib/const.py`, ne tento soubor.

Nastavení může přijít dvěma cestami:

  z prostředí     `from_environ()` — jedna konfigurace pro celou službu
  z adresy        `decode()` — `/c/<konfigurace>/manifest.json`, vlastní pro
                  každého, kdo si doplněk přidá; tak to dělá Stremio

Adresa z konfigurace se vyrábí na `/configure` a nese účty **v otevřené podobě**,
jen zakódované do base64. Není to šifra a nemá být — takhle fungují všechny
doplňky Stremia. Důsledek: doplněk nepatří na veřejnou adresu, dokud tomu
nerozumíš. Viz `pristupy.md` projektu.
"""
import base64
import json
import os

from .core.lib.const import LANGS, SORT_ORDERS

# proměnná prostředí → klíč nastavení, který čte engine
# TMDB se ve formuláři záměrně nenabízí: popisy a názvy si ve Stremiu řeší
# katalogový doplněk, ne my. Na dohledání souborů klíč vliv nemá — ověřeno na
# Pelíškách, které Cinemeta zná jako „Cosy Dens": český název dodá i veřejný
# katalog Sosáče, takže výsledek je s klíčem i bez něj stejný.
PROSTREDI = {
    "NOKTURNO_WS_USERNAME": "ws_username",
    "NOKTURNO_WS_PASSWORD": "ws_password",
    "NOKTURNO_STREAMUJ_USERNAME": "streamuj_username",
    "NOKTURNO_STREAMUJ_PASSWORD": "streamuj_password",
    "NOKTURNO_LUNA_URL": "luna_url",
    "NOKTURNO_LUNA_TOKEN": "luna_token",
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


# --- nastavení v adrese doplňku ------------------------------------------

def encode(options):
    """Nastavení do jednoho kousku adresy.

    Prázdné hodnoty se vynechají, klíče se řadí — stejné nastavení tak dá vždy
    stejnou adresu a uživateli se doplněk po přenastavení neduplikuje.
    """
    ulozit = {k: v for k, v in sorted((options or {}).items()) if v not in ("", None)}
    syrove = json.dumps(ulozit, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(syrove.encode("utf-8")).decode("ascii").rstrip("=")


def decode(kousek):
    """Zpátky na nastavení. Vrací None, když to nastavení není.

    Prochází přes `from_mapping()`, takže na neznámé klíče a nesmyslné hodnoty
    platí stejná pravidla jako u prostředí — z adresy je nelze podstrčit.
    """
    try:
        doplneni = "=" * (-len(kousek) % 4)
        data = json.loads(base64.urlsafe_b64decode(kousek + doplneni).decode("utf-8"))
    except Exception:  # noqa: BLE001 – cokoli nerozluštitelného prostě není nastavení
        return None
    return from_mapping(data) if isinstance(data, dict) else None


def fingerprint(options):
    """Krátký otisk nastavení — jméno složky s cache a klíč do cache enginů.

    Hesla se do něj nepromítají čitelně, takže může do logu i do jména složky.
    """
    import hashlib
    return hashlib.sha256(encode(options).encode("ascii")).hexdigest()[:16]


def sources_summary(engine):
    """Které zdroje jsou nastavené — do logu při startu a do manifestu.

    WebShare se hlásí podle vyplněných údajů, ne podle přihlášení; to je síťové
    volání a tohle se čte při startu i při každém dotazu na manifest.
    """
    zdroje = engine.sources()
    nazvy = {"luna": "Luna", "sosac": "Sosáč", "webshare": "WebShare",
             "hellspy": "HellSpy", "torrent": "torrenty"}
    return [nazvy[k] for k, zapnuto in zdroje.items() if zapnuto and k in nazvy]
