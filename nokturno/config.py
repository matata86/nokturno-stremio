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
doplňky Stremia. Adresu proto nikomu neposílat. Viz `pristupy.md` projektu.

**Luna je volitelná a jen z adresy doplňku** (formulář), ne z prostředí instance —
výchozí nastavení ji nemá, aby se streamy ze sdíleného `.env` nezdvojovaly s oficiálním
doplňkem Luny. Její odkazy vedou na server Luny, takže se přehrají jen tam, kde je
dosažitelný. Požadavek z internetu (`verejny`) s Lunou v soukromé síti ji nepoužije
(`bez_luny_v_domaci_siti`) — jinak by se přes veřejnou instanci dalo sahat do domácí sítě.
"""
import base64
import functools
import ipaddress
import json
import os
import re
import socket
import urllib.parse

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
    "NOKTURNO_HS_ENABLED": "hs_enabled",
    "NOKTURNO_ST_EMAIL": "st_email",
    "NOKTURNO_ST_PASSWORD": "st_password",
    "NOKTURNO_PREF_LANG": "pref_lang",
    "NOKTURNO_PREF_SURROUND": "pref_surround",
    "NOKTURNO_HIDE_SD": "hide_sd",
    "NOKTURNO_MAX_BITRATE": "max_bitrate_mbps",
    "NOKTURNO_SORT": "sort_streams",
}
# klíče, které jdou zadat jen adresou doplňku, ne proměnnou prostředí (viz docstring)
JEN_Z_ADRESY = ("luna_url", "luna_token")
POVOLENE = set(PROSTREDI.values()) | set(JEN_Z_ADRESY)
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
        if value is None or key not in POVOLENE:
            continue
        if key in LOGICKE:
            options[key] = str(value).strip().lower() in PRAVDA if isinstance(value, str) else bool(value)
        elif key == "max_bitrate_mbps":
            options[key] = _cislo(value)
        else:
            options[key] = str(value).strip()

    # do tokenu jde vložit celá adresa doplňku z Ruční instalace Luny — adresa serveru je v ní
    if options.get("luna_token") and not options.get("luna_url"):
        m = re.match(r"(https?://[^/]+)", str(options["luna_token"]))
        if m:
            options["luna_url"] = m.group(1)

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
             "hellspy": "HellSpy", "sledujteto": "Sledujteto", "torrent": "torrenty"}
    return [nazvy[k] for k, zapnuto in zdroje.items() if zapnuto and k in nazvy]


@functools.lru_cache(maxsize=256)
def _neverejny_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True   # nerozluštitelnou adresu radši nepoužít
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not ip.is_global:
            return True
    return False


def neverejna_adresa(url):
    """Míří adresa do soukromé sítě (LAN, loopback, Tailscale 100.64/10…)?"""
    host = urllib.parse.urlparse(str(url or "")).hostname
    return True if not host else _neverejny_host(host.lower())


@functools.lru_cache(maxsize=256)
def _host_v_lan(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return any(ipaddress.ip_address(str(i[4][0]).split("%")[0]).is_private for i in infos)


def adresa_v_lan(url):
    """Adresa v místní síti (192.168…, 10…, loopback) — ne Tailscale 100.64/10, ta je
    dosažitelná odkudkoli z tailnetu. Slouží jen k radě, proč se server na Lunu nedostal."""
    host = urllib.parse.urlparse(str(url or "")).hostname
    return bool(host) and _host_v_lan(host.lower())


def bez_luny_v_domaci_siti(options):
    """Požadavek z internetu: Luna v soukromé síti se vynechá — server by jinak na
    pokyn cizí adresy sahal do domácí sítě a odkazy Luny by se zvenku stejně nepřehrály."""
    if options and options.get("luna_token") and neverejna_adresa(options.get("luna_url")):
        return {k: v for k, v in options.items() if k not in JEN_Z_ADRESY}
    return options

