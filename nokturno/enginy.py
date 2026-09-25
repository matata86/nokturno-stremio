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
from .core.lib.store import Store
from . import cztor, sit

_LOGGER = logging.getLogger(__name__)

# Kolik různých nastavení držet naráz. Dvacet stačilo, dokud doplněk neměl víc
# uživatelů než tolik: 2026-09-17 se za 24 h objevilo 155 různých nastavení a jádra
# se protáčela — 439 vzniků za den, tedy každé v průměru 2,8× znovu, pokaždé s novým
# přihlášením ke zdrojům a studenou cache. Padesát pokrývá běžný souběh; v paměti to
# je pár set MB (kontejner má od 2026-09-17 2 GB místo 512 MB).
LIMIT = 50
NOVA_LIMIT = 20   # kolik jader „na zkoušku" (ještě nevrátila stream) se drží; viz `Enginy.povysit`
NOVA_JADRA_LIMIT = (30, 3600)   # nových jader z jedné adresy za hodinu — viz `PrilisMnohoNovych`
SPOLECNA_SLOZKA = "_spolecne"   # cache společná všem jádrům, viz `Enginy.spolecne`


# stropy pro procházení cizího úložiště z internetu (viz `_vytvor`)
STROPY_ULOZISTE = {"crawl_deadline": PUBLIC_CRAWL_DEADLINE, "max_dirs": PUBLIC_MAX_DIRS,
                   "timeout": PUBLIC_TIMEOUT}


class _Engine(Engine):
    """Jádro, kterému CZtor drží tokeny v trezoru zapečetěném klíčem z adresy (`cztor.py`),
    ne v otevřeném úložišti jádra jako v Kodi a HA."""

    def __init__(self, *args, cz_data_dir=None, **kwargs):
        self._cz_data_dir = cz_data_dir   # před super(): `Engine.__init__` už CZtor zkouší
        super().__init__(*args, **kwargs)

    def cztor_client(self):
        klic = self._opt("cz")
        if not klic or not self._cz_data_dir:
            return super().cztor_client()
        return cztor.klient(self._cz_data_dir, klic, cache=self.store)


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
        # Přehraj.to je ve Stremiu **per-uživatel** jako WebShare/Sledujteto: každý
        # zadá svůj účet ve formuláři, jádro si k němu postaví vlastní `PrehrajtoApi`
        # z `pt_email`/`pt_password` v nastavení (viz `Engine.pt`, `config.from_mapping`
        # odvodí `pt_enabled`). Bez účtu se zdroj nenabízí — anonymní HTML z jedné
        # serverové IP by dostalo 429, stejně jako Sledujteto potřebuje účet.
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
        # Společné úložiště všech jader — jen pro to, co na účtu nezávisí (TMDB, Cinemeta,
        # katalog Sosáče, hledání na HellSpy, hlavičky souborů z WebShare/HellSpy/FastShare;
        # co přesně, rozhoduje jádro, viz `Engine.shared`). Měřeno 2026-09-21 na LXC 124:
        # ze 42 000 souborů cache za den bylo 60 % kopií téhož v jiné složce — index Sosáče
        # (413 MB) se stahoval 50× denně, detail z TMDB dvakrát na každý titul a hlavičky
        # souborů se četly znovu pro každé nastavení, i když jsou vlastností souboru.
        # Název složky se schválně nedá splést s otiskem nastavení (16 hex znaků), aby ji
        # nesmazal `server.uklid_dat`. Zakládá se až s prvním jádrem — manifest bez
        # nastavení nemá na disku zanechat nic (viz `test_manifest_nezaklada_jadro`).
        self._spolecne = None

    @property
    def spolecne(self):
        # bez `self._zamek`: `_vytvor` sem chodí už pod ním z `pro()` a `Lock` není
        # reentrantní — první verze se tu zasekla navždy. Založení je idempotentní
        # (tatáž složka), souběh nanejvýš vyrobí dva objekty nad stejnými soubory.
        if self._spolecne is None:
            self._spolecne = Store(os.path.join(self.data_dir, SPOLECNA_SLOZKA))
        return self._spolecne

    def _vytvor(self, options, otisk, verejny):
        """Vlastní složka na nastavení — cache jednoho účtu nemá plnit výsledky druhého
        (tokeny, podepsané odkazy, sloučené streamy, vlastní úložiště); co je pro všechny
        stejné, jde do `self.spolecne`.
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
        return _Engine(options, slozka, opener=sit.OPENER if verejny else None,
                       storage_limits=STROPY_ULOZISTE if verejny else None,
                       shared_store=self.spolecne, cz_data_dir=self.data_dir)

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
