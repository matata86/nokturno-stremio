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

**Luna se ve Stremiu nepoužívá** (od 0.2.5). Má vlastní doplněk do Stremia, takže
by se soubory z WebShare zdvojovaly, a její odkazy vedou na server v domácí síti —
přes veřejnou adresu by nešly přehrát. WebShare zůstává, protože Nokturno dává
podepsaný odkaz rovnou na WebShare, který jde přehrát odkudkoli. Klíče Luny se
z prostředí ani z adresy nepřebírají; starší adresy, které je nesou, fungují dál.
"""
import base64
import json
import os
import re

from .core.lib.const import LANGS, SORT_ORDERS

# proměnná prostředí → klíč nastavení, který čte engine
# TMDB se ve formuláři záměrně nenabízí: popisy a názvy si ve Stremiu řeší
# katalogový doplněk, ne my. Na dohledání souborů klíč vliv nemá — ověřeno na
# Pelíškách, které Cinemeta zná jako „Cosy Dens": český název dodá i veřejný
# katalog Sosáče, takže výsledek je s klíčem i bez něj stejný.
# Proto tu `NOKTURNO_TMDB_KEY` **není**: klíč instance rozdává `Enginy` každému
# jádru zvlášť (viz `enginy.Enginy.__init__`), protože bez něj nejde přeložit
# `tmdb:` id od klientů. Do adresy s nastavením ani do formuláře nepatří.
PROSTREDI = {
    "NOKTURNO_WS_USERNAME": "ws_username",
    "NOKTURNO_WS_PASSWORD": "ws_password",
    "NOKTURNO_STREAMUJ_USERNAME": "streamuj_username",
    "NOKTURNO_STREAMUJ_PASSWORD": "streamuj_password",
    "NOKTURNO_HS_ENABLED": "hs_enabled",
    "NOKTURNO_ST_EMAIL": "st_email",
    "NOKTURNO_ST_PASSWORD": "st_password",
    "NOKTURNO_FS_USERNAME": "fs_username",
    "NOKTURNO_FS_PASSWORD": "fs_password",
    "NOKTURNO_PT_EMAIL": "pt_email",
    "NOKTURNO_PT_PASSWORD": "pt_password",
    # zapnuté katalogy, klíče oddělené čárkou — viz nokturno/katalogy.py
    "NOKTURNO_KATALOGY": "katalogy",
    "NOKTURNO_PREF_LANG": "pref_lang",
    "NOKTURNO_PREF_SURROUND": "pref_surround",
    "NOKTURNO_HIDE_SD": "hide_sd",
    "NOKTURNO_MAX_BITRATE": "max_bitrate_mbps",
    "NOKTURNO_SORT": "sort_streams",
    # vlastní úložiště (WebDAV), až tři — viz core/lib/storage_api.py
    **{f"NOKTURNO_DAV{n}_{pole.upper()}": f"dav{n}_{pole}"
       for n in (1, 2, 3) for pole in ("url", "username", "password", "name")},
}
PRAVDA = ("1", "true", "yes", "ano", "on")
# identita uživatele (`identita.py`) — jde jen z adresy, nikdy z prostředí; tvar hlídá `identita.TVAR`
ID_KLIC = "id"
ID_RE = re.compile(r"^(?:[0-9a-f]{8}\.)?[0-9a-f]{16}\.[0-9a-f]{16}$")   # s časem vydání (6.1.3) i bez
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
        if value is None or (key not in set(PROSTREDI.values()) and key != ID_KLIC):
            continue
        if key == ID_KLIC:
            if isinstance(value, str) and ID_RE.match(value.strip()):
                options[key] = value.strip()
            continue
        if key in LOGICKE:
            options[key] = str(value).strip().lower() in PRAVDA if isinstance(value, str) else bool(value)
        elif key == "max_bitrate_mbps":
            options[key] = _cislo(value)
        elif key == "katalogy":
            kusy = value if isinstance(value, (list, tuple)) else str(value).split(",")
            options[key] = ",".join(sorted({str(k).strip() for k in kusy if str(k).strip()}))
        else:
            options[key] = str(value).strip()

    # „nezáleží" nese formulář jako ANY: prázdnou hodnotu by z adresy zahodil a server
    # dosadil výchozí CZ (2026-09-14)
    if options.get("pref_lang") == "ANY":
        options["pref_lang"] = ""
    # nepovolená hodnota by v jádru propadla na výchozí, ale tiše — lepší ji srovnat tady
    if options.get("pref_lang") not in LANGS:
        options["pref_lang"] = ""
    if options.get("sort_streams") not in SORT_ORDERS:
        options["sort_streams"] = VYCHOZI["sort_streams"]
    # Přehraj.to je ve Stremiu per-uživatel jako ostatní zdroje — účet z adresy/prostředí.
    # Jádro zapíná zdroj přepínačem `pt_enabled`; ten se ve Stremiu odvodí z vyplněného
    # účtu (bez účtu API nevydá token a HTML z jedné serverové IP by dostalo 429, takže
    # anonymní režim jako v Kodi tu nedává smysl — nutný účet, stejně jako u Sledujteto).
    if str(options.get("pt_email") or "").strip() and str(options.get("pt_password") or "").strip():
        options["pt_enabled"] = True   # jen když je účet; jinak klíč vůbec není (čistý otisk)
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


def bez_lokalnich_uloziste(options, resolve=None):
    """Nastavení bez úložišť, na která se z internetu nesmí (viz `sit.zakazana`).

    Požadavek z internetu nese adresu úložiště od kohokoli. Doplněk pak tu adresu
    prochází a soubory z ní přes sebe streamuje, takže bez téhle pojistky by šlo
    přes Funnel sahat na služby, které poslouchají jen na localhostu (dashboard,
    Apache na :8080), na metadata cloudu (169.254.x), do domácí sítě (CoreELEC,
    Home Assistant) i do tailnetu. Uživatel zvenku na naši LAN stejně nedosáhne,
    takže o nic nepřijde; domácí požadavek se tudy nevede vůbec.

    Tohle je rychlé odmítnutí podle DNS v nastavení. Skutečnou pojistkou je
    `sit.verejny_opener()`, který hlídá adresu až při navázání spojení — DNS
    může podruhé vrátit něco jiného a cizí server může přesměrovat.
    """
    import urllib.parse

    from . import sit

    resolve = resolve or sit.resolvuj
    out = dict(options or {})
    for n in (1, 2, 3):
        url = str(out.get(f"dav{n}_url") or "").strip()
        if not url:
            continue
        host = urllib.parse.urlsplit(url if "://" in url else "http://" + url).hostname or ""
        try:
            adresy = list(resolve(host))
        except OSError:
            adresy = []
        if not adresy or any(sit.zakazana(a) for a in adresy):
            for pole in ("url", "username", "password", "name"):
                out.pop(f"dav{n}_{pole}", None)
    return out


def fingerprint(options):
    """Krátký otisk nastavení — jméno složky s cache a klíč do cache enginů.

    Hesla se do něj nepromítají čitelně, takže může do logu i do jména složky.
    """
    import hashlib
    return hashlib.sha256(encode(options).encode("ascii")).hexdigest()[:16]


NAZVY_ZDROJU = {"luna": "Luna", "sosac": "Sosáč", "webshare": "WebShare",
                "hellspy": "HellSpy", "sledujteto": "Sledujteto", "fastshare": "FastShare",
                "prehrajto": "Přehraj.to", "storage": "vlastní úložiště",
                "torrent": "torrenty"}


def sources_summary(engine):
    """Které zdroje jsou nastavené — do logu při startu.

    WebShare se hlásí podle vyplněných údajů, ne podle přihlášení; to je síťové volání.
    """
    return [NAZVY_ZDROJU[k] for k, zapnuto in engine.sources().items() if zapnuto and k in NAZVY_ZDROJU]


def ma_ucty(options):
    """Má nastavení vlastní přihlašovací údaje nebo úložiště? Jejich otisk (`fingerprint`)
    je pak jedinečný pro uživatele, takže na něj jde počítat limity místo sdílené IP.
    Nastavení jen s HellSpy a volbami sdílí spousta lidí — to takové není."""
    o = options or {}
    return any(str(o.get(k) or "").strip() for k in (
        "ws_username", "streamuj_username", "st_email", "fs_username", "pt_email",
        "dav1_url", "dav2_url", "dav3_url"))


def sources_from_options(options):
    """Totéž jen z nastavení, bez jádra — pro manifest. Manifest se dřív ptal jádra,
    a to znamenalo založit ho i se složkou na disku pro každou adresu, kterou kdo
    poslal (jeden GET na náhodný base64 = nová složka navždy; audit 2026-09-14)."""
    o = options or {}
    zapnuto = {
        "sosac": bool(str(o.get("streamuj_username") or "").strip()),
        "webshare": bool(str(o.get("ws_username") or "").strip()),
        "hellspy": bool(o.get("hs_enabled")),
        "sledujteto": bool(str(o.get("st_email") or "").strip()),
        "fastshare": bool(str(o.get("fs_username") or "").strip()),
        "prehrajto": bool(str(o.get("pt_email") or "").strip()),
        "storage": any(str(o.get(f"dav{n}_url") or "").strip() for n in (1, 2, 3)),
    }
    return [NAZVY_ZDROJU[k] for k, v in zapnuto.items() if v]
