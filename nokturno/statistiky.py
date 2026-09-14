"""Anonymní statistiky doplňku pro Stremio — stejný sběrný bod jako Kodi a HA.

Jedna instalace = jedno nastavení doplňku (otisk adresy), ne jeden server: kdo si
doplněk přidal s vlastní adresou, má vlastní složku jádra (`Enginy`) a v ní
vlastní `stats.json` s náhodným id. Z adresy se nic neposílá — ani účty, ani
otisk. Jde jen verze, které zdroje má to nastavení zapnuté a u kterých titulů se
otevřely streamy (`core/lib/stats.py`), nejvýš jednou za 6 hodin.

Vypnout jde proměnnou prostředí `NOKTURNO_STATS=0`. I pak se ale při otevření
streamů nejvýš jednou za 6 hodin pošle ping — jen náhodné id, produkt a verze,
aby bylo vidět, že nastavení žije. Tituly ani zdroje ne.
"""
import logging
import os
import threading

from .core.engine import split_episode_id
from .core.lib.stats import COLLECT_URL, Stats

_LOGGER = logging.getLogger(__name__)
VYPNUTO = ("0", "false", "ne", "no", "off")


class Statistiky:
    def __init__(self, verze, zapnuto=True, url=COLLECT_URL):
        self.verze = verze
        self.zapnuto = zapnuto
        self.url = url
        self._zamek = threading.Lock()
        self._stats = {}

    @classmethod
    def z_prostredi(cls, verze, environ=None):
        env = os.environ if environ is None else environ
        return cls(verze, zapnuto=str(env.get("NOKTURNO_STATS", "1")).strip().lower() not in VYPNUTO)

    def zaznamenej(self, engine, ctype, item_id):
        """Po zobrazení streamů — na pozadí, odpověď Stremiu kvůli tomu nečeká."""
        cil = self.zpracuj if self.zapnuto else self.ping
        threading.Thread(target=cil, args=(engine,) if cil == self.ping else (engine, ctype, item_id),
                         daemon=True, name="nokturno-statistiky").start()

    def _pro(self, engine):
        slozka = engine.store.dir
        with self._zamek:
            stats = self._stats.get(slozka)
            if stats is None:
                stats = self._stats[slozka] = Stats(slozka)
        return stats

    def ping(self, engine):
        """Vypnuté statistiky: jen „nastavení žije" (id, produkt, verze). Nikdy nevyhodí výjimku."""
        try:
            stats = self._pro(engine)
            with self._zamek:
                if not stats.due():
                    return
                ok, why = stats.send(self.url, version=self.verze, agent="Stremio nokturno",
                                     product="stremio", ping=True)
            if not ok:
                _LOGGER.info("ping neodeslán: %s", why)
        except Exception as err:  # noqa: BLE001 – statistiky nesmí nic shodit
            _LOGGER.debug("ping: %s", err)

    def zpracuj(self, engine, ctype, item_id):
        """Synchronní část (vlákno výš, testy přímo). Nikdy nevyhodí výjimku."""
        try:
            stats = self._pro(engine)
            title, year, kind = self._titul(engine, ctype, item_id)
            with self._zamek:
                stats.note_play(item_id, title, year, kind)
                if not stats.due():
                    return
                zdroje = [k for k, v in engine.sources().items() if v]
                ok, why = stats.send(self.url, version=self.verze, platform="Stremio", lang="cs",
                                     agent="Stremio nokturno", sources=zdroje, product="stremio")
            if not ok:
                _LOGGER.info("statistiky neodeslány: %s", why)
        except Exception as err:  # noqa: BLE001 – statistiky nesmí nic shodit
            _LOGGER.debug("statistiky: %s", err)

    @staticmethod
    def _titul(engine, ctype, item_id):
        _base, season, _episode = split_episode_id(item_id)
        kind = "series" if season is not None or ctype == "series" else "movie"
        try:
            meta, video = engine.meta(ctype, item_id)
        except Exception:  # noqa: BLE001 – název je jen pro čitelnost přehledu
            return "", None, kind
        title = (video or {}).get("title") or meta.get("_title") or meta.get("name") or ""
        year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
        return title, (int(year) if year.isdigit() else None), kind
