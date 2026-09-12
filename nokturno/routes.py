"""Endpointy protokolu Stremia nad jádrem Nokturna.

    GET /manifest.json            co doplněk umí
    GET /stream/:type/:id.json    streamy k titulu
    GET /play/:payload            302 na skutečný soubor
    GET /health                   pro kontejner

Odkazy WebShare a HellSpy platí jen chvíli a nesou podpis, takže se nedávají
rovnou do odpovědi. Stremio dostane adresu na `/play/`, která soubor rozklíčuje
až ve chvíli, kdy se na ni přehrávač skutečně obrátí.
"""
import logging
import urllib.parse

from .core.engine import NokturnoError, split_episode_id
from . import mapping

_LOGGER = logging.getLogger(__name__)

VERZE = "0.1.0"
TYPY = ("movie", "series")


class Odpoved:
    """Co server pošle klientovi."""

    def __init__(self, status=200, data=None, location=None, text=None):
        self.status = status
        self.data = data
        self.location = location
        self.text = text

    @property
    def body(self):
        if self.data is not None:
            return mapping.json_bytes(self.data), "application/json; charset=utf-8"
        return (self.text or "").encode("utf-8"), "text/plain; charset=utf-8"


def chyba(status, zprava):
    return Odpoved(status=status, text=zprava)


class Router:
    """Drží engine a základ adresy, pod kterou je služba vidět."""

    def __init__(self, engine, zdroje=(), verze=VERZE):
        self.engine = engine
        self.zdroje = list(zdroje)
        self.verze = verze

    # --- adresy -----------------------------------------------------------
    def _odkaz(self, zaklad):
        """Stavitel adres na `/play/` pro daný požadavek.

        Základ musí být absolutní: Stremio přehrává na jiném zařízení, než na
        kterém běží tahle služba, takže relativní cesta by mu nic neřekla.
        """
        def odkaz(vnitrni_url):
            return f"{zaklad}/play/{mapping.zakoduj(vnitrni_url)}"
        return odkaz

    # --- endpointy --------------------------------------------------------
    def manifest(self):
        return Odpoved(data=mapping.manifest(self.verze, self.zdroje, nastaveno=bool(self.zdroje)))

    def health(self):
        return Odpoved(data={"ok": True, "verze": self.verze, "zdroje": self.zdroje})

    def uvod(self, zaklad):
        radky = [
            "Nokturno pro Stremio",
            "",
            f"verze:   {self.verze}",
            f"zdroje:  {', '.join(self.zdroje) if self.zdroje else 'žádný nenastavený'}",
            "",
            "Doplněk se do Stremia přidá touhle adresou:",
            f"  {zaklad}/manifest.json",
        ]
        if not self.zdroje:
            radky += ["", "Bez nastaveného zdroje doplněk žádné streamy nenajde.",
                      "Účty se předávají proměnnými NOKTURNO_* — viz README."]
        return Odpoved(text="\n".join(radky) + "\n")

    def streams(self, ctype, item_id, zaklad):
        if ctype not in TYPY:
            return chyba(404, f"Neznámý typ obsahu: {ctype}")
        base_id, season, episode = split_episode_id(item_id)
        if not base_id.startswith("tt"):
            # manifest hlásí idPrefixes ["tt"], takže sem nic jiného chodit nemá
            return Odpoved(data={"streams": []})
        if ctype == "series" and season is None:
            return chyba(400, "U seriálu čekám id ve tvaru tt…:sezóna:díl")

        try:
            popisy = self.engine.streams(ctype, item_id)
        except NokturnoError as err:
            # chybějící zdroj není chyba služby; Stremio má ukázat prázdno a jít dál
            _LOGGER.info("streamy %s %s: %s", ctype, item_id, err)
            return Odpoved(data={"streams": []})
        except Exception as err:  # noqa: BLE001 – výpadek zdroje nesmí shodit službu
            _LOGGER.warning("streamy %s %s selhaly: %s", ctype, item_id, err)
            return Odpoved(data={"streams": []})

        _LOGGER.info("streamy %s %s: %d", ctype, item_id, len(popisy))
        return Odpoved(data=mapping.streams_response(popisy, self._odkaz(zaklad)))

    def play(self, payload):
        vnitrni = mapping.dekoduj(payload)
        if not vnitrni:
            return chyba(400, "Neplatný odkaz.")
        try:
            skutecna = self.engine.resolve(vnitrni)
        except NokturnoError as err:
            # nejčastěji „soubor je dočasně nedostupný“ od WebShare
            _LOGGER.info("rozklíčování %s: %s", vnitrni[:40], err)
            return chyba(502, str(err))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("rozklíčování %s selhalo: %s", vnitrni[:40], err)
            return chyba(502, "Zdroj teď odkaz nevydal.")
        if not skutecna:
            return chyba(502, "Zdroj vrátil prázdný odkaz.")
        return Odpoved(status=302, location=skutecna, text="")

    # --- rozcestník -------------------------------------------------------
    def route(self, cesta, zaklad):
        """Cesta požadavku na odpověď. `zaklad` je absolutní adresa služby."""
        cesta = urllib.parse.unquote(cesta.split("?", 1)[0])
        if cesta in ("", "/"):
            return self.uvod(zaklad)
        if cesta == "/manifest.json":
            return self.manifest()
        if cesta == "/health":
            return self.health()

        casti = [c for c in cesta.split("/") if c]
        if casti[0] == "play" and len(casti) == 2:
            return self.play(casti[1])
        if casti[0] == "stream" and len(casti) == 3 and casti[2].endswith(".json"):
            return self.streams(casti[1], casti[2][:-len(".json")], zaklad)
        return chyba(404, "Tady nic není. Doplněk se přidává adresou /manifest.json")
