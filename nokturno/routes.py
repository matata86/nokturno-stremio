"""Endpointy protokolu Stremia nad jádrem Nokturna.

    GET /configure                       formulář, který vyrobí adresu s účty
    GET /c/<nastavení>/manifest.json     co doplněk umí
    GET /c/<nastavení>/stream/:t/:id.json   streamy k titulu
    GET /c/<nastavení>/play/:payload     302 na skutečný soubor
    GET /health                          pro kontejner

Stremio nemá soubor nastavení — účty se nosí zakódované v cestě adresy, takže
každý, kdo si doplněk přidá, má vlastní. Server si nic nepamatuje a hledá vždy
pod účtem toho, kdo se ptá.

Adresy bez `/c/<nastavení>/` fungují dál a berou nastavení z prostředí. Drží to
při životě instance nasazené dřív, než tahle vrstva vznikla.

Odkazy WebShare a HellSpy platí jen chvíli a nesou podpis, takže se nedávají
rovnou do odpovědi. Stremio dostane adresu na `/play/`, která soubor rozklíčuje
až ve chvíli, kdy se na ni přehrávač skutečně obrátí — a protože nese tentýž
prefix, rozklíčuje ho pod správným účtem.
"""
import logging
import pathlib
import urllib.parse

from .core.engine import NokturnoError, is_sosac_id, split_episode_id
from . import config, mapping

_LOGGER = logging.getLogger(__name__)

VERZE = "0.2.1"
TYPY = ("movie", "series")
STATIKA = pathlib.Path(__file__).resolve().parent / "static"


class Odpoved:
    """Co server pošle klientovi."""

    def __init__(self, status=200, data=None, location=None, text=None, html=None):
        self.status = status
        self.data = data
        self.location = location
        self.text = text
        self.html = html

    @property
    def body(self):
        if self.data is not None:
            return mapping.json_bytes(self.data), "application/json; charset=utf-8"
        if self.html is not None:
            return self.html.encode("utf-8"), "text/html; charset=utf-8"
        return (self.text or "").encode("utf-8"), "text/plain; charset=utf-8"


def chyba(status, zprava):
    return Odpoved(status=status, text=zprava)


class Router:
    """Obsluha požadavků. Jádro si bere podle nastavení v adrese."""

    def __init__(self, enginy, verze=VERZE, predvyplnit=False):
        self.enginy = enginy
        self.verze = verze
        # nabídnout ve formuláři účty z prostředí? Na sdílené instanci NE — ukázalo
        # by je komukoli, kdo formulář otevře. Na vlastní ušetří opisování hashů.
        self.predvyplnit = predvyplnit

    # --- adresy -----------------------------------------------------------
    @staticmethod
    def _rozdel(cesta):
        """`/c/<nastavení>/zbytek` → (nastavení, `/zbytek`); jinak (None, cesta)."""
        casti = [c for c in cesta.split("/") if c]
        if len(casti) >= 2 and casti[0] == "c":
            return casti[1], "/" + "/".join(casti[2:])
        return None, cesta

    def _odkaz(self, zaklad, kousek):
        """Stavitel adres na `/play/`, se stejným nastavením jako příchozí požadavek.

        Základ musí být absolutní: Stremio přehrává na jiném zařízení, než na
        kterém běží tahle služba. Prefix musí sedět, jinak by se soubor
        rozklíčoval cizím účtem, nebo vůbec.
        """
        predpona = f"{zaklad}/c/{kousek}" if kousek else zaklad

        def odkaz(vnitrni_url):
            return f"{predpona}/play/{mapping.zakoduj(vnitrni_url)}"
        return odkaz

    # --- endpointy --------------------------------------------------------
    def manifest(self, engine, nastaveno):
        zdroje = config.sources_summary(engine)
        data = mapping.manifest(self.verze, zdroje, nastaveno=bool(zdroje))
        data["behaviorHints"]["configurable"] = True
        # bez vlastního nastavení ať Stremio rovnou nabídne formulář
        data["behaviorHints"]["configurationRequired"] = not (nastaveno or zdroje)
        return Odpoved(data=data)

    def health(self):
        return Odpoved(data={"ok": True, "verze": self.verze, "jader": len(self.enginy)})

    def configure(self, kousek, zaklad):
        """Formulář, který vyrobí adresu s účty. Předvyplní se z adresy, na které stojí."""
        try:
            html = (STATIKA / "configure.html").read_text(encoding="utf-8")
        except OSError:
            return chyba(500, "Formulář nastavení chybí.")
        soucasne = config.decode(kousek) if kousek else None
        if soucasne is None and self.predvyplnit:
            soucasne = self.enginy.vychozi_options
        html = html.replace("__NASTAVENI__", mapping.json_bytes(soucasne or {}).decode("utf-8"))
        html = html.replace("__ZAKLAD__", zaklad)
        html = html.replace("__VERZE__", self.verze)
        return Odpoved(html=html)

    def uvod(self, engine, zaklad):
        zdroje = config.sources_summary(engine)
        radky = [
            "Nokturno pro Stremio", "",
            f"verze:  {self.verze}",
            f"zdroje: {', '.join(zdroje) if zdroje else 'žádný nenastavený'}",
            "",
            "Doplněk se přidává adresou, kterou vyrobí formulář:",
            f"  {zaklad}/configure",
        ]
        return Odpoved(text="\n".join(radky) + "\n")

    def streams(self, engine, ctype, item_id, zaklad, kousek):
        if ctype not in TYPY:
            return chyba(404, f"Neznámý typ obsahu: {ctype}")
        base_id, season, _episode = split_episode_id(item_id)
        if not (base_id.startswith("tt") or is_sosac_id(base_id)):
            # titul z cizího katalogu, jehož id neumíme přeložit na název — hledat
            # fulltextem není podle čeho. Zapíšeme si, co chodí: kdyby se nějaký
            # tvar opakoval, vyplatí se ho podpořit.
            _LOGGER.info("neznámý tvar id, vracím prázdno: %s", item_id[:60])
            return Odpoved(data={"streams": []})
        if ctype == "series" and season is None:
            return chyba(400, "U seriálu čekám id ve tvaru tt…:sezóna:díl")

        try:
            # jen přísná shoda, stejně jako v Kodi a HA: volnější fulltext si má
            # uživatel vyžádat sám (tlačítko v Kodi), ve Stremiu na to není místo
            popisy = engine.streams(ctype, item_id)
        except NokturnoError as err:
            # chybějící zdroj není chyba služby; Stremio má ukázat prázdno a jít dál
            _LOGGER.info("streamy %s %s: %s", ctype, item_id, err)
            return Odpoved(data={"streams": []})
        except Exception as err:  # noqa: BLE001 – výpadek zdroje nesmí shodit službu
            _LOGGER.warning("streamy %s %s selhaly: %s", ctype, item_id, err)
            return Odpoved(data={"streams": []})

        _LOGGER.info("streamy %s %s: %d", ctype, item_id, len(popisy))
        return Odpoved(data=mapping.streams_response(popisy, self._odkaz(zaklad, kousek)))

    def play(self, engine, payload):
        vnitrni = mapping.dekoduj(payload)
        if not vnitrni:
            return chyba(400, "Neplatný odkaz.")
        try:
            skutecna = engine.resolve(vnitrni)
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
        if cesta == "/health":
            return self.health()

        kousek, zbytek = self._rozdel(cesta)
        options = config.decode(kousek) if kousek else None
        if kousek and options is None:
            return chyba(404, "Adresa nese nečitelné nastavení. Vyrob si novou na /configure")

        if zbytek in ("", "/", "/configure", "/configure/"):
            if zbytek in ("/configure", "/configure/"):
                return self.configure(kousek, zaklad)
            return self.uvod(self.enginy.pro(options), zaklad)

        engine = self.enginy.pro(options)
        if zbytek == "/manifest.json":
            return self.manifest(engine, nastaveno=bool(kousek))

        casti = [c for c in zbytek.split("/") if c]
        if casti and casti[0] == "play" and len(casti) == 2:
            return self.play(engine, casti[1])
        if casti and casti[0] == "stream" and len(casti) == 3 and casti[2].endswith(".json"):
            return self.streams(engine, casti[1], casti[2][:-len(".json")], zaklad, kousek)
        return chyba(404, "Tady nic není. Doplněk se nastavuje na /configure")
