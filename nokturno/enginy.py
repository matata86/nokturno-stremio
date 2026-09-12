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

_LOGGER = logging.getLogger(__name__)

LIMIT = 20   # kolik různých nastavení držet naráz


class Enginy:
    """Jádra podle otisku nastavení, nejdéle nepoužité vypadne."""

    def __init__(self, data_dir, vychozi_options=None, limit=LIMIT):
        self.data_dir = data_dir
        self.vychozi_options = vychozi_options or {}
        self.limit = limit
        self._cache = OrderedDict()
        self._zamek = threading.Lock()

    def _vytvor(self, options, otisk):
        """Vlastní složka na nastavení — cache jednoho účtu nemá plnit výsledky druhého."""
        slozka = os.path.join(self.data_dir, otisk)
        os.makedirs(slozka, exist_ok=True)
        _LOGGER.info("nové jádro pro nastavení %s", otisk)
        return Engine(options, slozka)

    def pro(self, options=None):
        """Jádro pro dané nastavení; bez nastavení to výchozí z prostředí."""
        if options is None:
            options = self.vychozi_options
        otisk = fingerprint(options)
        with self._zamek:
            engine = self._cache.get(otisk)
            if engine is None:
                engine = self._vytvor(options, otisk)
                self._cache[otisk] = engine
                while len(self._cache) > self.limit:
                    stary, _ = self._cache.popitem(last=False)
                    _LOGGER.info("zahazuji nepoužívané jádro %s", stary)
            else:
                self._cache.move_to_end(otisk)
        return engine

    def __len__(self):
        return len(self._cache)
