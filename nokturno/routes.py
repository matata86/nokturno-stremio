"""Endpointy protokolu Stremia nad jádrem Nokturna.

    GET /                                úvodní stránka a rozcestník repozitářů
    GET /configure                       formulář, který vyrobí adresu s účty
    GET /c/<nastavení>/manifest.json     co doplněk umí
    GET /c/<nastavení>/stream/:t/:id.json   streamy k titulu
    GET /c/<nastavení>/play/:payload     302 na skutečný soubor
    GET /c/<nastavení>/check             ověření účtů pro formulář (WebShare + VIP)
    GET /health                          pro kontejner

Stremio nemá soubor nastavení — účty se nosí zakódované v cestě adresy, takže
každý, kdo si doplněk přidá, má vlastní. Server si nic nepamatuje a hledá vždy
pod účtem toho, kdo se ptá.

Adresy bez `/c/<nastavení>/` fungují dál a berou nastavení z prostředí. Drží to
při životě instance nasazené dřív, než tahle vrstva vznikla. **Jen ze soukromé
sítě:** požadavek z internetu (Tailscale Funnel, viz `server.je_verejny`) bez
vlastního nastavení v adrese dostane 403 a formulář se mu nepředvyplní — jinak by
hledal a stahoval pod účty majitele instance a formulář by mu je ukázal.

Odkazy WebShare a HellSpy platí jen chvíli a nesou podpis, takže se nedávají
rovnou do odpovědi. Stremio dostane adresu na `/play/`, která soubor rozklíčuje
až ve chvíli, kdy se na ni přehrávač skutečně obrátí — a protože nese tentýž
prefix, rozklíčuje ho pod správným účtem.

Výjimkou jsou vlastní úložiště (`dav:`) a FastShare (`fs:`): ty chtějí u každého
požadavku autentizační hlavičku, takže se vydávají jako přímá adresa zdroje
s `behaviorHints.proxyHeaders` (viz `mapping.stream_object`). Data pak tečou ze
zdroje rovnou ke klientovi a tenhle server se jich nedotkne — do 5.2.25 šla přes
něj a byl tím fakticky veřejná proxy pro cizí úložiště.
"""
import html as html_lib
import logging
import pathlib
import threading
import time
import urllib.parse

from .core.engine import NokturnoError, is_sosac_id, split_episode_id
from .core.lib.webshare_api import WebshareApi, WebshareError
from .core.lib.sledujteto_api import SledujtetoApi
from .core.lib.fastshare_api import FastshareApi
from .core.lib.storage_api import SLOTS, StorageApi
from . import config, mapping, sit

_LOGGER = logging.getLogger(__name__)

VERZE = "5.2.26"
TYPY = ("movie", "series")
CHECK_LIMIT = (10, 5 * 60)   # ověření účtů z jedné adresy za 5 minut — jinak je /check relay pro hádání hesel


class Okno:
    """Počítadlo v klouzavém okně per klíč (adresa klienta)."""

    def __init__(self, limit, window_s, max_keys=5000):
        self.limit, self.window_s, self.max_keys = limit, window_s, max_keys
        self._data = {}
        self._zamek = threading.Lock()

    def povolit(self, key):
        now = time.time()
        with self._zamek:
            if len(self._data) > self.max_keys:
                self._data.clear()
            count, start = self._data.get(key, (0, now))
            if now - start > self.window_s:
                count, start = 0, now
            if count >= self.limit:
                return False
            self._data[key] = (count + 1, start)
            return True
STATIKA = pathlib.Path(__file__).resolve().parent / "static"
JAZYKY = ("cs", "sk")   # stránky úvodu a formuláře; manifest a streamy zůstávají česky


def jazyk_z_hlavicky(accept_language):
    """Jazyk stránek podle `Accept-Language`: slovenština, jen když je první volbou prohlížeče.

    Bere se nejvyšší `q` (při shodě dřívější položka), `q=0` znamená „nechci".
    Cokoli jiného, prázdná nebo rozbitá hlavička → čeština.
    """
    kandidati = []
    for poradi, cast in enumerate((accept_language or "").split(",")):
        kusy = [k.strip() for k in cast.split(";")]
        tag = kusy[0].lower()
        if not tag:
            continue
        q = 1.0
        for parametr in kusy[1:]:
            if parametr.lower().startswith("q="):
                try:
                    q = float(parametr[2:])
                except ValueError:
                    q = 0.0
        if q > 0:
            kandidati.append((-q, poradi, tag))
    if not kandidati:
        return "cs"
    return "sk" if min(kandidati)[2].split("-")[0] == "sk" else "cs"


def klient_z_useragent(user_agent):
    """Která appka se ptá na streamy — jen pro statistiky (Dashboard, sloupec „Kodi / HA").

    Nuvio a Streamlet posílají vlastní jednoznačný `User-Agent` (`Nuvio/x.y.z-beta`,
    `Streamlet/x.y.z (…)`) — ověřeno z provozu 2026-09-17 (dočasné logování v `server.py`,
    121 požadavků, žádná kolize s ničím jiným). Oficiální Stremio appka UA občas neposílá
    vůbec, jindy jde jen UA prohlížeče (web verze na PC/smart TV) nebo generický klient
    (`okhttp`, komunitní agregátor `AIOStreams`) — ty všechny od sebe spolehlivě nejdou
    odlišit, spadají tedy pod „stremio"."""
    ua = user_agent or ""
    if ua.startswith("Nuvio/"):
        return "nuvio"
    if ua.startswith("Streamlet/"):
        return "streamlet"
    return "stremio"


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


NEUSPECHU_DOST = 3   # kolik selhání jednoho zdroje v jedné odpovědi stačí, než to vzdáme


def _primy(engine):
    """`vnitřní odkaz → (adresa, hlavičky)` pro zdroje z `mapping.PRES_HLAVICKY`.

    None znamená „tenhle soubor teď přehrát nejde" (nenastavený účet, vypršelé
    přihlášení, málo kreditu) — stream se pak vůbec nenabídne, protože bez
    hlaviček by stejně neodehrál.

    Selhání se počítají: FastShare se při nedostatku kreditu zkusí přihlásit
    znovu (kredit se mohl mezitím dobít) a to je síťový dotaz — u výpisu s
    desítkami souborů by se opakoval pro každý z nich.
    """
    neuspechy = {}

    def primy(vnitrni):
        klic = vnitrni.split(":", 1)[0]
        if neuspechy.get(klic, 0) >= NEUSPECHU_DOST:
            return None
        try:
            return engine.file_request(vnitrni)
        except NokturnoError as err:
            _LOGGER.info("přímý odkaz %s: %s", vnitrni[:40], err)
        except Exception as err:  # noqa: BLE001 – výpadek zdroje nesmí shodit výpis streamů
            _LOGGER.warning("přímý odkaz %s selhal: %s", vnitrni[:40], err)
        neuspechy[klic] = neuspechy.get(klic, 0) + 1
        return None

    return primy


class Router:
    """Obsluha požadavků. Jádro si bere podle nastavení v adrese."""

    def __init__(self, enginy, verze=VERZE, predvyplnit=False, statistiky=None, katalogy=None):
        self.enginy = enginy
        self.katalogy = katalogy   # nokturno.katalogy.Katalogy, None = katalogy se nenabízejí
        self.verze = verze
        # nabídnout ve formuláři účty z prostředí? Na sdílené instanci NE — ukázalo
        # by je komukoli, kdo formulář otevře. Na vlastní ušetří opisování hashů.
        self.predvyplnit = predvyplnit
        self.statistiky = statistiky   # nokturno.statistiky.Statistiky, None = vypnuto
        self.ws_api = WebshareApi   # testy podstrčí falešné, aby nešly na síť
        self.st_api = SledujtetoApi
        self.fs_api = FastshareApi
        self.dav_api = StorageApi
        self.check_okno = Okno(*CHECK_LIMIT)

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
    def manifest(self, options, nastaveno):
        """Jen z nastavení — jádro se kvůli manifestu nezakládá (viz `sources_from_options`)."""
        zdroje = config.sources_from_options(options)
        katalogy = self.katalogy.manifest(options) if self.katalogy else []
        data = mapping.manifest(self.verze, zdroje, nastaveno=bool(zdroje), katalogy=katalogy)
        data["behaviorHints"]["configurable"] = True
        # bez vlastního nastavení ať Stremio rovnou nabídne formulář
        data["behaviorHints"]["configurationRequired"] = not (nastaveno or zdroje)
        return Odpoved(data=data)

    def health(self):
        return Odpoved(data={"ok": True, "verze": self.verze, "jader": len(self.enginy)})

    @staticmethod
    def _stranka(jmeno, jazyk):
        """`configure` + `sk` → `configure.sk.html`; když jazyková verze chybí, česká."""
        if jazyk and jazyk != "cs":
            try:
                return (STATIKA / f"{jmeno}.{jazyk}.html").read_text(encoding="utf-8")
            except OSError:
                pass
        return (STATIKA / f"{jmeno}.html").read_text(encoding="utf-8")

    def configure(self, kousek, zaklad, verejny=False, jazyk="cs"):
        """Formulář, který vyrobí adresu s účty. Předvyplní se z adresy, na které stojí."""
        try:
            html = self._stranka("configure", jazyk)
        except OSError:
            return chyba(500, "Formulář nastavení chybí.")
        soucasne = config.decode(kousek) if kousek else None
        if soucasne is None and self.predvyplnit and not verejny:
            soucasne = self.enginy.vychozi_options
        # hodnoty z adresy jsou od kohokoli — do <script> jen escapované (viz json_do_scriptu)
        html = html.replace("__NASTAVENI__", mapping.json_do_scriptu(soucasne or {}))
        html = html.replace("__KATALOGY__", mapping.json_do_scriptu(self.katalogy.formular(jazyk) if self.katalogy else []))
        html = html.replace("__ZAKLAD__", html_lib.escape(zaklad, quote=True))
        html = html.replace("__VERZE__", self.verze)
        return Odpoved(html=html)

    def check(self, options, verejny=False):
        """Ověření účtů pro tlačítko ve formuláři.

        WebShare se opravdu přihlásí a řekne, kolik zbývá VIP — bez VIP je rychlost
        omezená a film se nedá plynule přehrát, což je nejčastější „nic nehraje".
        Streamuj přihlašovací endpoint nemá, špatné heslo se pozná až při přehrání,
        takže se jen ohlásí, co je vyplněné. Jádro se kvůli tomu nezakládá — jen
        jedno přihlášení, žádná cache.

        Z internetu (`verejny`) se úložiště prochází hlídaným openerem a chyba se
        nehlásí doslova: „connection refused" vs. „timed out" z adres v naší síti
        by z tlačítka udělalo skener portů.
        """
        out = {"webshare": None, "streamuj": None, "sledujteto": None, "fastshare": None,
               "hellspy": bool(options.get("hs_enabled"))}
        user = (options.get("ws_username") or "").strip()
        if user:
            try:
                api = self.ws_api(user, options.get("ws_password") or "")
                if not api.login():
                    raise WebshareError("přihlášení selhalo")
                stav = api.account_status()
                out["webshare"] = {"ok": True, "vip": bool(stav.get("vip")),
                                   "days": int(stav.get("days") or 0), "until": stav.get("until") or ""}
            except Exception as err:  # noqa: BLE001 – cokoli, co přihlášení zastaví, je pro uživatele totéž
                _LOGGER.info("ověření WebShare %s: %s", user[:3] + "…", err)
                out["webshare"] = {"ok": False, "chyba": str(err) or "přihlášení selhalo"}
        if (options.get("streamuj_username") or "").strip():
            out["streamuj"] = {"heslo": bool((options.get("streamuj_password") or "").strip())}
        email = (options.get("st_email") or "").strip()
        if email:
            # Sledujteto: přihlášení a jestli má účet Premium — bez něj přehrání neprojde
            try:
                user = self.st_api(email, options.get("st_password") or "").me()
                out["sledujteto"] = {"ok": True, "premium": bool(user.get("is_premium"))}
            except Exception as err:  # noqa: BLE001 – pro uživatele je každé selhání totéž
                _LOGGER.info("ověření Sledujteto %s: %s", email[:3] + "…", err)
                out["sledujteto"] = {"ok": False, "chyba": str(err) or "přihlášení selhalo"}
        fs_user = (options.get("fs_username") or "").strip()
        if fs_user:
            # FastShare: přihlášení a kolik zbývá — přehrání se odečítá z kreditu, pokud účet nemá neomezený tarif
            try:
                ucet = self.fs_api(fs_user, options.get("fs_password") or "").login()
                out["fastshare"] = {"ok": True, "neomezene": bool(ucet.get("unlimited")),
                                    "kredit_mb": int(ucet.get("credit_mb") or 0)}
            except Exception as err:  # noqa: BLE001 – pro uživatele je každé selhání totéž
                _LOGGER.info("ověření FastShare %s: %s", fs_user[:3] + "…", err)
                out["fastshare"] = {"ok": False, "chyba": str(err) or "přihlášení selhalo"}
        out["uloziste"] = []
        for n in range(1, SLOTS + 1):
            url = (options.get(f"dav{n}_url") or "").strip()
            if not url:
                continue
            # jen kořen složky, celý strom se prochází až při hledání
            try:
                navic = {"opener": sit.OPENER} if verejny else {}
                api = self.dav_api(url, options.get(f"dav{n}_username") or "", options.get(f"dav{n}_password") or "",
                                   options.get(f"dav{n}_name") or "", slot=n, **navic)
                out["uloziste"].append({"slot": n, "ok": True, "polozek": api.check()})
            except Exception as err:  # noqa: BLE001 – pro uživatele je každé selhání totéž
                _LOGGER.info("ověření úložiště %d: %s", n, err)
                out["uloziste"].append({"slot": n, "ok": False,
                                        "chyba": "nedostupné" if verejny else (str(err) or "nedostupné")})
        return Odpoved(data=out)

    def uvod(self, zaklad, jazyk="cs"):
        """Úvodní stránka a rozcestník celé rodiny Nokturna — nic o nastavení instance neprozradí."""
        try:
            html = self._stranka("index", jazyk)
        except OSError:
            return Odpoved(text=f"Nokturno pro Stremio {self.verze}\nNastavení: {zaklad}/configure\n")
        return Odpoved(html=html.replace("__ZAKLAD__", html_lib.escape(zaklad, quote=True))
                       .replace("__VERZE__", self.verze))

    def streams(self, engine, ctype, item_id, zaklad, kousek, aplikace="stremio"):
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
        if self.statistiky is not None:
            self.statistiky.zaznamenej(engine, ctype, item_id, aplikace)
        return Odpoved(data=mapping.streams_response(popisy, self._odkaz(zaklad, kousek),
                                                      primy=_primy(engine)))

    def katalog(self, casti):
        """`/catalog/<typ>/<id>.json` nebo `/catalog/<typ>/<id>/skip=<n>.json` → `{"metas": [...]}`."""
        if self.katalogy is None or len(casti) not in (3, 4) or not casti[-1].endswith(".json"):
            return chyba(404, "Takový katalog tu není.")
        typ = casti[1]
        if len(casti) == 3:
            katalog_id, extra = casti[2][:-len(".json")], ""
        else:
            katalog_id, extra = casti[2], casti[3][:-len(".json")]
        skip = (urllib.parse.parse_qs(extra).get("skip") or ["0"])[0]
        metas = self.katalogy.polozky(typ, katalog_id, skip)
        if metas is None:
            return chyba(404, "Takový katalog tu není.")
        return Odpoved(data={"metas": metas})

    def play(self, engine, payload, klic="vychozi"):
        vnitrni = mapping.dekoduj(payload)
        if not vnitrni:
            return chyba(400, "Neplatný odkaz.")
        if vnitrni.startswith(mapping.PRES_HLAVICKY):
            # Vlastní úložiště a FastShare chtějí u každého požadavku hlavičku (heslo,
            # cookie z přihlášení). Do 5.2.25 je soubor tekl přes tenhle server, od 5.2.26
            # se vydává přímá adresa zdroje s `behaviorHints.proxyHeaders` (viz
            # `mapping.stream_object`) — hlavičky posílá přehrávač sám. Sem se dostane jen
            # odkaz uložený ve starém „pokračovat ve sledování"; ten se musí načíst znovu.
            return chyba(410, "Tenhle odkaz už neplatí — otevři titul znovu a vyber stream.")
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
    def route(self, cesta, zaklad, verejny=False, jazyk=None, klient="", aplikace="stremio"):
        """Cesta požadavku na odpověď. `zaklad` je absolutní adresa služby,
        `verejny` říká, že přišel z internetu (viz docstring modulu), `jazyk`
        je jazyk stránek z `Accept-Language` (viz `jazyk_z_hlavicky`), `klient`
        adresa klienta pro limit na `/check`, `aplikace` appka podle User-Agentu
        (viz `klient_z_useragent`) pro statistiky u `/stream/`.
        Parametr `?lang=cs|sk` v adrese má přednost, bez obojího čeština."""
        cesta, _, dotaz = cesta.partition("?")
        lang = (urllib.parse.parse_qs(dotaz).get("lang") or [""])[0].strip().lower().split("-")[0]
        if lang in JAZYKY:
            jazyk = lang
        elif jazyk not in JAZYKY:
            jazyk = "cs"
        cesta = urllib.parse.unquote(cesta)
        if cesta == "/health":
            return self.health()

        kousek, zbytek = self._rozdel(cesta)
        options = config.decode(kousek) if kousek else None
        if kousek and options is None:
            return chyba(404, "Adresa nese nečitelné nastavení. Vyrob si novou na /configure")
        if verejny and options:
            options = config.bez_lokalnich_uloziste(options)

        if zbytek in ("", "/", "/configure", "/configure/"):
            if zbytek in ("/configure", "/configure/"):
                return self.configure(kousek, zaklad, verejny, jazyk)
            return self.uvod(zaklad, jazyk)

        if verejny and not kousek:
            if zbytek == "/manifest.json":
                # Stremio z toho pozná, že si má doplněk nejdřív nastavit
                data = mapping.manifest(self.verze, (), nastaveno=False)
                data["behaviorHints"].update(configurable=True, configurationRequired=True)
                return Odpoved(data=data)
            return chyba(403, f"Doplněk bez vlastního nastavení jde použít jen z domácí sítě. "
                              f"Vyrob si adresu na {zaklad}/configure")

        if zbytek == "/check":
            if not self.check_okno.povolit(klient or "?"):
                return chyba(429, "Příliš mnoho ověření za sebou, zkus to za pár minut.")
            return self.check(options if kousek else self.enginy.vychozi_options, verejny=verejny)
        if zbytek == "/manifest.json":
            return self.manifest(options if kousek else self.enginy.vychozi_options, nastaveno=bool(kousek))

        casti = [c for c in zbytek.split("/") if c]
        if casti and casti[0] == "catalog":
            # katalog na účtech nezávisí — jádro se nezakládá, cache je jedna pro všechny
            return self.katalog(casti)

        engine = self.enginy.pro(options, verejny=verejny)

        if casti and casti[0] == "play" and len(casti) == 2:
            return self.play(engine, casti[1], klic=config.fingerprint(options) if kousek else "vychozi")
        if casti and casti[0] == "stream" and len(casti) == 3 and casti[2].endswith(".json"):
            return self.streams(engine, casti[1], casti[2][:-len(".json")], zaklad, kousek, aplikace)
        return chyba(404, "Tady nic není. Doplněk se nastavuje na /configure")
