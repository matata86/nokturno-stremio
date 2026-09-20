"""Jádra podle nastavení.

Každá adresa doplňku nese vlastní nastavení, takže jeden proces obsluhuje víc
konfigurací najednou. Vytvářet `Engine` na každý požadavek nejde — přihlašuje se
k WebShare a drží cache — proto se drží stranou a sdílí mezi požadavky.

Cache je omezená a nejdéle nepoužité jádro vypadne. Bez limitu by adresa
s překlepem v hesle založila jádro navždy.
"""
import logging
import os
import threading
import time
from collections import OrderedDict

from .config import fingerprint
from .core.engine import Engine
from .core.lib.storage_api import PUBLIC_CRAWL_DEADLINE, PUBLIC_MAX_DIRS, PUBLIC_TIMEOUT
from . import sit

_LOGGER = logging.getLogger(__name__)

# Kolik různých nastavení držet naráz. Dvacet stačilo, dokud doplněk neměl víc
# uživatelů než tolik: 2026-09-17 se za 24 h objevilo 155 různých nastavení a jádra
# se protáčela — 439 vzniků za den, tedy každé v průměru 2,8× znovu, pokaždé s novým
# přihlášením ke zdrojům a studenou cache. Padesát pokrývá běžný souběh; v paměti to
# je pár set MB (kontejner má od 2026-09-17 2 GB místo 512 MB).
LIMIT = 50
NOVA_LIMIT = 20   # kolik jader „na zkoušku" (ještě nevrátila stream) se drží; viz `Enginy.povysit`
NOVA_JADRA_LIMIT = (30, 3600)   # nových jader z jedné adresy za hodinu — viz `PrilisMnohoNovych`


# stropy pro procházení cizího úložiště z internetu (viz `_vytvor`)
STROPY_ULOZISTE = {"crawl_deadline": PUBLIC_CRAWL_DEADLINE, "max_dirs": PUBLIC_MAX_DIRS,
                   "timeout": PUBLIC_TIMEOUT}


class PrilisMnohoNovych(Exception):
    """Adresa za hodinu založila víc nových jader, než je rozumné. Bot, který posílá požadavky
    s vymyšlenými nastaveními, by jinak vytlačil z paměti jádra skutečných uživatelů (limit
    `LIMIT` je malý, nejdéle nepoužité vypadne) a každý by začínal se studenou cache."""


class _Okno:
    """Počítadlo v pevném okně na klíč (totéž co `routes.Okno`, jen bez závislosti na routes)."""

    def __init__(self, limit, okno_s, max_klicu=5000):
        self.limit, self.okno_s, self.max_klicu = limit, okno_s, max_klicu
        self._data = {}
        self._zamek = threading.Lock()

    def povolit(self, klic):
        now = time.time()
        with self._zamek:
            if klic not in self._data and len(self._data) >= self.max_klicu:
                # jen prošlá okna; když je i pak plno, nový klíč se odmítne (dřív se mazalo vše)
                self._data = {k: v for k, v in self._data.items() if v[1] > now - self.okno_s}
                if len(self._data) >= self.max_klicu:
                    return False
            pocet, start = self._data.get(klic, (0, now))
            if now - start > self.okno_s:
                pocet, start = 0, now
            if pocet >= self.limit:
                return False
            self._data[klic] = (pocet + 1, start)
            return True


class Enginy:
    """Jádra podle otisku nastavení, nejdéle nepoužité vypadne.

    Dva oddělené seznamy: **ověřená** jádra (`limit`; vrátila aspoň jeden stream, nebo jde o výchozí
    nastavení z prostředí) a **nová** na zkoušku (`nova_limit`). Nové jádro se do ověřených
    povýší až v `povysit`, tedy po prvním vráceném streamu. Bot s vymyšlenými nastaveními tak
    vytlačuje jen jiná nová jádra a skutečným uživatelům nesahá na jejich rozehranou cache.
    """

    def __init__(self, data_dir, vychozi_options=None, limit=LIMIT, tmdb_key="", nova_jadra=NOVA_JADRA_LIMIT,
                 nova_limit=NOVA_LIMIT):
        self.data_dir = data_dir
        self._nova_okno = _Okno(*nova_jadra)
        self.vychozi_options = vychozi_options or {}
        self.limit = limit
        # Klíč TMDB instance (`NOKTURNO_TMDB_KEY`) — dostane ho **každé** jádro, i to
        # pro požadavek z internetu. Je to výjimka z pravidla „veřejný požadavek nedostane
        # nastavení z prostředí" (viz `server.je_verejny`), a je vědomá: to pravidlo chrání
        # účty, na které se stahuje (WebShare, Streamuj — placené, cizí by na ně jely).
        # Klíč TMDB je zdarma, jen na čtení veřejných metadat, klientovi se nikdy nevydá
        # a bez něj nejde přeložit `tmdb:` id od klientů (viz `routes._imdb_z_tmdb`) —
        # tedy ani najít streamy k titulu z TMDB katalogu.
        self.tmdb_key = tmdb_key or ""
        self.nova_limit = nova_limit
        self._cache = OrderedDict()   # ověřená
        self._nova = OrderedDict()    # na zkoušku
        self._zamek = threading.Lock()

    def _vytvor(self, options, otisk, verejny):
        """Vlastní složka na nastavení — cache jednoho účtu nemá plnit výsledky druhého.
        Jádro pro požadavek z internetu dostane hlídaný opener (viz `sit`) a stropy
        na procházení cizího úložiště: adresa WebDAV je v nastavení doplňku, takže si
        ji kdokoli může nasměrovat na server, který na každý PROPFIND odpovídá pomalu
        a vrací stále nové podsložky — bez stropu by jeden požadavek na streamy držel
        osm vláken hodiny (audit 2026-09-19, nález 5)."""
        slozka = os.path.join(self.data_dir, otisk)
        os.makedirs(slozka, exist_ok=True)
        _LOGGER.info("nové jádro pro nastavení %s%s", otisk, " (z internetu)" if verejny else "")
        if self.tmdb_key:
            # až za otiskem: klíč je pro všechna nastavení stejný, nemá tříštit cache
            options = {**options, "tmdb_api_key": self.tmdb_key}
        return Engine(options, slozka, opener=sit.OPENER if verejny else None,
                      storage_limits=STROPY_ULOZISTE if verejny else None)

    def pro(self, options=None, verejny=False, klient=""):
        """Jádro pro dané nastavení; bez nastavení to výchozí z prostředí.

        Veřejné a domácí jádro téhož nastavení jsou dvě: liší se tím, kam se smí
        připojit. Složku s cache sdílejí, obsah je stejný.

        Se `klient` (klíč adresy) se počítá, kolik nových jader adresa založila; už
        existující jádro se neomezuje nikdy. Nad limit `PrilisMnohoNovych`.
        """
        if options is None:
            options = self.vychozi_options
        otisk = fingerprint(options)
        klic = (otisk, bool(verejny))
        with self._zamek:
            for seznam in (self._cache, self._nova):
                engine = seznam.get(klic)
                if engine is not None:
                    seznam.move_to_end(klic)
                    return engine
            if klient and not self._nova_okno.povolit(klient):
                raise PrilisMnohoNovych(klient)
            engine = self._vytvor(options, otisk, verejny)
            if options is self.vychozi_options:
                # výchozí nastavení z prostředí je vždy ověřené — nikdo cizí ho nevymyslí
                self._cache[klic] = engine
                self._oriznout(self._cache, self.limit)
            else:
                self._nova[klic] = engine
                self._oriznout(self._nova, self.nova_limit)
        return engine

    def povysit(self, options=None, verejny=False):
        """Jádro vrátilo stream → z „na zkoušku" do ověřených (tam ho bot nevytlačí)."""
        if options is None:
            return
        klic = (fingerprint(options), bool(verejny))
        with self._zamek:
            engine = self._nova.pop(klic, None)
            if engine is not None:
                self._cache[klic] = engine
                self._oriznout(self._cache, self.limit)

    @staticmethod
    def _oriznout(seznam, limit):
        while len(seznam) > limit:
            stary, _ = seznam.popitem(last=False)
            _LOGGER.info("zahazuji nepoužívané jádro %s", stary)

    def __len__(self):
        return len(self._cache) + len(self._nova)
