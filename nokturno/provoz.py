"""Hlášení provozu do dashboardu (obrazovka Provoz).

Doplněk běží na LXC 124 vedle dashboardu jako samostatná služba, takže do jeho
databáze nesahá — posílá jen dávky na `POST /traffic` (localhost, sdílený token
`NOKTURNO_TRAFFIC_TOKEN`). Bez tokenu je celá věc tiše vypnutá a nic se nikam
neposílá; nasazení bez dashboardu tím pádem funguje dál.

Co se posílá: čas, služba (`stremio` / `prehravani`), **normalizovaná** cesta,
metoda, stav, appka podle User-Agentu, odeslané bajty a doba obsluhy. Co se
neposílá: adresa klienta (kromě odmítnutých požadavků, viz níž) a prefix `/c/<nastavení>/` — ten nese účty, a i kdyby
odsud omylem prošel, server ho ještě jednou přepíše (`traffic.prijmi_davku`).

Výjimka: odmítnuté požadavky (zablokované nastavení, překročený limit) se do provozu
nepočítají vůbec — jdou jako souhrn `abuse` po (adresa, nastavení, důvod) s počtem zásahů,
User-Agentem a prvním/posledním časem, ať dashboard ukáže, kdo na doplněk tluče. Nastavení
se posílá jen jako zkrácený otisk, ne obsah.

Kdyby dashboard neodpovídal, dávka se zahodí a pokračuje se dál — statistika
provozu nesmí zdržet ani jeden požadavek přehrávače.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import config

_LOGGER = logging.getLogger(__name__)

VYCHOZI_URL = "http://127.0.0.1:8080/traffic"
VYPNUTO = ("0", "false", "ne", "no", "off")
INTERVAL = 30           # jak často se fronta odesílá (s)
STROP = 5000            # víc řádků než tohle se zahazuje (dashboard neodpovídá)
DAVKA = 1000            # nejvíc řádků v jednom požadavku
STROP_UTOKU = 500       # nejvíc různých (adresa, nastavení, důvod) ve frontě


def klasifikuj(cesta):
    """Cesta → (služba, cesta bez proměnných částí).

    `/play/` je vlastní služba, i když přes ni od 5.2.26 tečou jen přesměrování
    (302) — data zdrojů jdou ke klientovi přímo, mimo tenhle server.
    """
    holá = (cesta or "/").split("?", 1)[0]
    zbytek = re.sub(r"^/c/[^/?]+", "", holá)
    prefix = ""
    if zbytek != holá:
        # rozlišení pro přehled „kolik lidí ještě nemá identitu“ (bez ověření podpisu)
        nastaveni = config.decode(holá.split("/")[2])
        if nastaveni and nastaveni.get(config.ID_KLIC):
            prefix = "/c/{nastaveni s identitou}"
        elif nastaveni and config.ma_ucty(nastaveni):
            prefix = "/c/{nastaveni s účty}"
        else:
            prefix = "/c/{nastaveni}"   # sdílené nastavení bez účtů — ti, kdo mají přejít na identitu
    casti = [c for c in zbytek.split("/") if c]
    if not casti:
        return "stremio", (prefix + "/") if prefix else "/"
    prvni = casti[0]
    if prvni == "play":
        return "prehravani", prefix + "/play"
    if prvni in ("stream", "meta", "catalog", "subtitles"):
        # typ (movie/series) je užitečný, id titulu ne
        typ = casti[1] if len(casti) > 1 and casti[1] in ("movie", "series") else "{typ}"
        return "stremio", f"{prefix}/{prvni}/{typ}"
    if prvni in ("manifest.json", "configure", "check", "health"):
        return "stremio", f"{prefix}/{prvni}"
    if prvni == "static":
        return "stremio", f"{prefix}/static/{{soubor}}"
    return "stremio", f"{prefix}/{prvni}"[:200]


class Provoz:
    """Fronta v paměti + vlákno, které ji jednou za `interval` odešle."""

    def __init__(self, url=VYCHOZI_URL, token="", interval=INTERVAL, strop=STROP):
        self.url = url
        self.token = token
        self.interval = interval
        self.strop = strop
        self.zapnuto = bool(token)
        self.zahozeno = 0
        self._fronta = []
        self._utoky = {}
        self._zprava = ""
        self._zamek = threading.Lock()
        self._vlakno = None
        self._konec = threading.Event()

    @classmethod
    def z_prostredi(cls, environ=None):
        env = os.environ if environ is None else environ
        token = str(env.get("NOKTURNO_TRAFFIC_TOKEN", "")).strip()
        if str(env.get("NOKTURNO_TRAFFIC", "1")).strip().lower() in VYPNUTO:
            token = ""
        return cls(url=env.get("NOKTURNO_TRAFFIC_URL", VYCHOZI_URL).strip() or VYCHOZI_URL, token=token)

    def zaznamenej(self, cesta, metoda, stav, bajty_ven=0, doba_ms=0, aplikace="stremio"):
        if not self.zapnuto:
            return
        sluzba, tvar = klasifikuj(cesta)
        with self._zamek:
            if len(self._fronta) >= self.strop:
                self.zahozeno += 1
                return
            self._fronta.append({
                "ts": int(time.time()), "service": sluzba, "route": tvar,
                "method": (metoda or "GET")[:10], "status": int(stav or 0),
                "client": aplikace, "bytes_out": max(0, int(bajty_ven)),
                "dur_ms": max(0, int(doba_ms)),
            })

    def zaznamenej_utok(self, ip, ua, fp, duvod, ma_id=None):
        """Odmítnutý požadavek → jen počítadlo, žádný řádek provozu."""
        if not self.zapnuto:
            return
        klic = (str(ip or "?")[:64], (fp or "")[:16], str(duvod)[:20])
        with self._zamek:
            u = self._utoky.get(klic)
            if u is None:
                if len(self._utoky) >= STROP_UTOKU:
                    return
                u = self._utoky[klic] = {"hits": 0, "first": int(time.time())}
            u["hits"] += 1
            u["last"] = int(time.time())
            u["ua"] = str(ua or "")[:120]
            u["has_id"] = -1 if ma_id is None else int(ma_id)

    def zprava(self):
        """Zpráva z dashboardu pro uživatele Stremia (obrazovka Zprávy); prázdné = žádná.
        Čte se z paměti, obnovuje ji vlákno provozu — požadavek na streamy na síť nečeká."""
        return self._zprava

    def _nacti_zpravu(self):
        adresa = self.url.rsplit("/", 1)[0] + "/traffic/message" if self.url.endswith("/traffic") else self.url + "/message"
        req = urllib.request.Request(adresa, headers={"X-Nokturno-Token": self.token, "User-Agent": "Nokturno provoz"})
        try:
            with urllib.request.urlopen(req, timeout=5) as odp:
                data = json.loads(odp.read(20000).decode("utf-8"))
            text = data.get("text") if isinstance(data, dict) else ""
            self._zprava = " ".join(str(text or "").split())[:300]
        except (urllib.error.URLError, OSError, ValueError) as err:
            _LOGGER.debug("zpráva z dashboardu se nenačetla: %s", err)   # zůstává poslední známá

    def start(self):
        if not self.zapnuto or self._vlakno is not None:
            return
        self._vlakno = threading.Thread(target=self._smycka, daemon=True, name="nokturno-provoz")
        self._vlakno.start()

    def stop(self):
        self._konec.set()

    def _smycka(self):
        while not self._konec.wait(self.interval):
            try:
                self.odesli()
                self._nacti_zpravu()
            except Exception as err:  # noqa: BLE001 – hlášení provozu nesmí nic shodit
                _LOGGER.debug("provoz: %s", err)

    def odesli(self):
        """Odešle frontu (po dávkách). Vrací, kolik řádků odešlo. Nikdy nevyhodí výjimku."""
        with self._zamek:
            fronta, self._fronta = self._fronta, []
            utoky, self._utoky = self._utoky, {}
        if utoky:
            self._posli_davku([], [{"ip": k[0], "fp": k[1], "reason": k[2], **v} for k, v in utoky.items()])
        odeslano = 0
        for i in range(0, len(fronta), DAVKA):
            davka = fronta[i:i + DAVKA]
            if not self._posli_davku(davka):
                # dashboard neodpovídá — zbytek zahodíme, ať fronta neroste do paměti
                self.zahozeno += len(fronta) - odeslano
                return odeslano
            odeslano += len(davka)
        return odeslano

    def _posli_davku(self, davka, utoky=None):
        telo = json.dumps({"events": davka, "abuse": utoky or []}).encode("utf-8")
        req = urllib.request.Request(self.url, data=telo, method="POST", headers={
            "Content-Type": "application/json",
            "X-Nokturno-Token": self.token,
            "User-Agent": "Nokturno provoz",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as odp:
                return 200 <= getattr(odp, "status", odp.code) < 300
        except (urllib.error.URLError, OSError, ValueError) as err:
            _LOGGER.debug("provoz neodeslán: %s", err)
            return False
