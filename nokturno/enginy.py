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
from collections import OrderedDict

from .config import fingerprint
from .core.engine import Engine
from . import sit

_LOGGER = logging.getLogger(__name__)

# Kolik různých nastavení držet naráz. Dvacet stačilo, dokud doplněk neměl víc
# uživatelů než tolik: 2026-09-17 se za 24 h objevilo 155 různých nastavení a jádra
# se protáčela — 439 vzniků za den, tedy každé v průměru 2,8× znovu, pokaždé s novým
# přihlášením ke zdrojům a studenou cache. Padesát pokrývá běžný souběh; v paměti to
# je pár set MB (kontejner má od 2026-09-17 2 GB místo 512 MB).
LIMIT = 50


class Enginy:
    """Jádra podle otisku nastavení, nejdéle nepoužité vypadne."""

    def __init__(self, data_dir, vychozi_options=None, limit=LIMIT):
        self.data_dir = data_dir
        self.vychozi_options = vychozi_options or {}
        self.limit = limit
        self._cache = OrderedDict()
        self._zamek = threading.Lock()

    def _vytvor(self, options, otisk, verejny):
        """Vlastní složka na nastavení — cache jednoho účtu nemá plnit výsledky druhého.
        Jádro pro požadavek z internetu dostane hlídaný opener (viz `sit`)."""
        slozka = os.path.join(self.data_dir, otisk)
        os.makedirs(slozka, exist_ok=True)
        _LOGGER.info("nové jádro pro nastavení %s%s", otisk, " (z internetu)" if verejny else "")
        return Engine(options, slozka, opener=sit.OPENER if verejny else None)

    def pro(self, options=None, verejny=False):
        """Jádro pro dané nastavení; bez nastavení to výchozí z prostředí.

        Veřejné a domácí jádro téhož nastavení jsou dvě: liší se tím, kam se smí
        připojit. Složku s cache sdílejí, obsah je stejný.
        """
        if options is None:
            options = self.vychozi_options
        otisk = fingerprint(options)
        klic = (otisk, bool(verejny))
        with self._zamek:
            engine = self._cache.get(klic)
            if engine is None:
                engine = self._vytvor(options, otisk, verejny)
                self._cache[klic] = engine
                while len(self._cache) > self.limit:
                    stary, _ = self._cache.popitem(last=False)
                    _LOGGER.info("zahazuji nepoužívané jádro %s", stary)
            else:
                self._cache.move_to_end(klic)
        return engine

    def __len__(self):
        return len(self._cache)
