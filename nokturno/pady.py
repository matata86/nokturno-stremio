"""Hlášení o pádech služby do dashboardu (obrazovka Pády) — stejný formát jako Kodi.

Neošetřená výjimka při obsluze požadavku (`server.Handler.do_GET`) se přes
`core/lib/crash.py` zařadí do fronty v `<data>/pady/` a vlákno ji hned odešle.
Z hlášení se mažou adresy, účty a nastavení z cesty (`/c/<nastavení>`); do logu
jde jen posledních pár záznamů loggeru `nokturno`, i ty přes `scrub`.

Id instalace je jedno na server (`<data>/pady/id`), ne na nastavení doplňku jako
u statistik — pád je vlastnost kódu a serveru, ne toho, kdo se ptal. Vypnout jde
proměnnou prostředí `NOKTURNO_CRASH_REPORTS=0`.
"""
import collections
import logging
import os
import re
import threading
import uuid

from .core.lib.crash import CRASH_URL, CrashReporter

_LOGGER = logging.getLogger(__name__)
VYPNUTO = ("0", "false", "ne", "no", "off")
LOG_ZAZNAMU = 60


class _PosledniZaznamy(logging.Handler):
    """Kruhová paměť posledních záznamů loggeru `nokturno` — kontext k hlášení."""

    def __init__(self, kolik=LOG_ZAZNAMU):
        super().__init__(level=logging.INFO)
        self.zaznamy = collections.deque(maxlen=kolik)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record):
        try:
            self.zaznamy.append(self.format(record))
        except Exception:  # noqa: BLE001 – logování nesmí nic shodit
            pass


def akce_z_cesty(path):
    """„/c/<nastavení>/stream/movie/tt1.json" → „stream/movie" — co se dělo, bez id a účtů."""
    cesta = re.sub(r"^/c/[^/?]+", "", (path or "").split("?", 1)[0])
    casti = [c for c in cesta.split("/") if c][:2]
    if len(casti) == 2 and "." in casti[1]:
        casti = casti[:1]
    return "/".join(casti) or "/"


class Pady:
    def __init__(self, data_dir, verze, zapnuto=True, url=CRASH_URL):
        self.verze = verze
        self.zapnuto = zapnuto
        self.url = url
        self.slozka = os.path.join(data_dir, "pady")
        self.reporter = CrashReporter(self.slozka)
        self.log = _PosledniZaznamy()
        self._id = None
        if zapnuto:
            logging.getLogger("nokturno").addHandler(self.log)

    @classmethod
    def z_prostredi(cls, data_dir, verze, environ=None):
        env = os.environ if environ is None else environ
        zapnuto = str(env.get("NOKTURNO_CRASH_REPORTS", "1")).strip().lower() not in VYPNUTO
        return cls(data_dir, verze, zapnuto=zapnuto)

    def install_id(self):
        if self._id:
            return self._id
        cesta = os.path.join(self.slozka, "id")
        try:
            with open(cesta, encoding="utf-8") as f:
                ulozene = f.read().strip()
            if re.fullmatch(r"[0-9a-f]{32}", ulozene):
                self._id = ulozene
                return ulozene
        except OSError:
            pass
        self._id = uuid.uuid4().hex
        try:
            os.makedirs(self.slozka, exist_ok=True)
            with open(cesta, "w", encoding="utf-8") as f:
                f.write(self._id)
        except OSError:
            pass
        return self._id

    def zaznamenej(self, exc, path):
        """Zařadí hlášení a odešle frontu na pozadí. Nikdy nevyhodí výjimku."""
        if not self.zapnuto:
            return False
        try:
            zarazeno = self.reporter.capture(exc, self.install_id(), "stremio", self.verze, platform="server",
                                             action=akce_z_cesty(path), log_lines=list(self.log.zaznamy))
        except Exception:  # noqa: BLE001
            return False
        if zarazeno:
            self.odesli()
        return zarazeno

    def odesli(self):
        if not self.zapnuto:
            return

        def run():
            sent, left = self.reporter.flush(self.url, agent=f"nokturno-stremio/{self.verze}")
            if sent or left:
                _LOGGER.info("hlášení o pádech: odesláno %d, zbývá %d", sent, left)

        threading.Thread(target=run, daemon=True, name="nokturno-pady").start()
