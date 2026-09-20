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
import ipaddress
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
from .enginy import PrilisMnohoNovych
from .identita import Identita

_LOGGER = logging.getLogger(__name__)

VERZE = "6.4.8"
TYPY = ("movie", "series")
CHECK_LIMIT = (10, 5 * 60)   # ověření účtů z jedné adresy za 5 minut — jinak je /check relay pro hádání hesel
# streamy z jedné IP klienta (IPv6 po /64, viz `klic_klienta`). Reálná data 2026-09-19: medián
# 2 titulů za den, 99. percentil 37, nejvíc 61; bot procházející katalog jich dělal stovky za
# vteřiny a nafoukl cache na 400 000 souborů. 60 za 10 min člověk nepřekročí, bot ano hned.
# Dřív se počítalo per otisk nastavení — jenže nastavení bez účtů (jen HellSpy a volby) sdílí
# spousta lidí, takže jeden bot vyčerpal limit, respektive blokaci, všem ostatním.
# 2026-09-20: z 60 na 120 — domácnost se dvěma zařízeními na staré adrese bez identity sdílí
# jeden klíč (IP) a Stremio/Nuvio žádají o streamy hromadně; bot (~28/s) ho překročí stejně.
STREAM_LIMIT = (120, 10 * 60)
ID_LIMIT = (3, 3600)   # vydaných identit z jedné adresy za hodinu (formulář /configure)
# Audit 2026-09-19: `/play/` neměl limit vůbec (sto tisíc rozklíčování z jedné adresy = HellSpy
# 429 a jádro ho pak vypne všem na 10 minut), `/catalog` taky ne (každý `skip` = nový dotaz na
# TMDB/Sosáč). A s identitou v adrese se limity počítaly **místo** IP, takže N identit z jedné
# adresy = N × limit — proto je nad tím strop na IP (IPv6 po /64), který identita neobejde;
# je volnější než limit na uživatele, aby CGNAT s pár lidmi za jednou adresou nevadil.
PLAY_LIMIT = (120, 10 * 60)
KATALOG_LIMIT = (300, 10 * 60)
IP_STROP = (300, 10 * 60)
MAX_SKIP = 500


def klic_klienta(adresa):
    """Klíč do limitu: IPv4 celá, IPv6 jen prefix /64 — ten má jedna domácnost či stroj
    celý, takže by si bot jinak mohl adresu uvnitř něj měnit s každým požadavkem."""
    try:
        ip = ipaddress.ip_address((adresa or "").strip())
    except ValueError:
        return adresa or ""
    if ip.version == 6 and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class Okno:
    """Počítadlo v klouzavém okně per klíč (adresa klienta).

    Při přetečení `max_keys` se vyhazují jen prošlá okna; když je i pak plno, nový klíč
    se odmítne — dřív se slovník mazal celý, takže 5001 různých /64 z jednoho IPv6 /48
    vynulovalo limity všem (audit 2026-09-19)."""

    def __init__(self, limit, window_s, max_keys=5000):
        self.limit, self.window_s, self.max_keys = limit, window_s, max_keys
        self._data = {}
        self._zamek = threading.Lock()

    def _uklid(self, now):
        hranice = now - self.window_s
        self._data = {k: v for k, v in self._data.items() if v[1] > hranice}

    def povolit(self, key):
        now = time.time()
        with self._zamek:
            if key not in self._data and len(self._data) >= self.max_keys:
                self._uklid(now)
                if len(self._data) >= self.max_keys:
                    return False
            count, start = self._data.get(key, (0, now))
            if now - start > self.window_s:
                count, start = 0, now
            if count >= self.limit:
                return False
            self._data[key] = (count + 1, start)
            return True


class Blokace:
    """Krátká automatická blokace adres, které opakovaně narážejí na limity streamů.

    Limit `STREAM_LIMIT` (na adresu, IPv6 po /64) bota jen zpomalí, ale nezastaví — dál mu
    odpovídá 429 a on tluče dál (v incidentu 2026-09-19 ~60 požadavků/s). Proto se počítají odmítnutí:
    `PRAH` za `okno_s` a adresa dostane na `doba_s` rovnou 403 bez další práce. Stav je jen v paměti (restart ho vynuluje) a týká se jen
    `/stream/` a `/play/` — jiné cesty se nikdy neblokují, adresu může sdílet víc lidí (CGNAT).
    Při přetečení `max_klicu` se vyhazují jen prošlé záznamy; blokace se nikdy nemažou naráz.
    """

    def __init__(self, prah=20, okno_s=10 * 60, doba_s=3600, max_klicu=5000, soubor=None):
        self.prah, self.okno_s, self.doba_s, self.max_klicu = prah, okno_s, doba_s, max_klicu
        self._odmitnuti = {}
        self._blok = {}
        self._kolikrat = {}
        self._zamek = threading.Lock()
        # identity (klíč `id:…`) odebrané natrvalo — druhá blokace téže identity; soubor přežije restart
        self.soubor = soubor
        self.odebrane = set()
        if soubor:
            try:
                with open(soubor, encoding="utf-8") as f:
                    self.odebrane = {r.strip() for r in f if r.strip()}
            except OSError:
                pass

    def odebrana(self, klic):
        return klic in self.odebrane

    def _odebrat(self, klic):
        self.odebrane.add(klic)
        if self.soubor:
            try:
                with open(self.soubor, "a", encoding="utf-8") as f:
                    f.write(klic + "\n")
            except OSError:
                _LOGGER.warning("odebrané identity se nepodařilo zapsat do %s", self.soubor)

    def blokovana(self, ip):
        if ip in self.odebrane:
            return True
        with self._zamek:
            do = self._blok.get(ip)
            if do is None:
                return False
            if time.time() >= do:
                del self._blok[ip]
                return False
            return True

    def prohresek(self, ip):
        """Zaznamená odmítnutí; vrátí True, když tím adresa právě dostala blokaci."""
        now = time.time()
        with self._zamek:
            if ip not in self._odmitnuti and len(self._odmitnuti) >= self.max_klicu:
                hranice = now - self.okno_s
                self._odmitnuti = {k: v for k, v in self._odmitnuti.items() if v[1] > hranice}
                if len(self._odmitnuti) >= self.max_klicu:
                    return False   # plno i po úklidu: nový klíč se nepočítá, staré blokace zůstávají
            pocet, start = self._odmitnuti.get(ip, (0, now))
            if now - start > self.okno_s:
                pocet, start = 0, now
            pocet += 1
            self._odmitnuti[ip] = (pocet, start)
            if pocet >= self.prah and ip not in self._blok:
                if len(self._blok) >= self.max_klicu:
                    self._blok = {k: do for k, do in self._blok.items() if do > now}
                    if len(self._blok) >= self.max_klicu:
                        return False
                self._blok[ip] = now + self.doba_s
                if ip.startswith("id:"):
                    self._kolikrat[ip] = self._kolikrat.get(ip, 0) + 1
                    if self._kolikrat[ip] >= 2:
                        self._odebrat(ip)   # identita podruhé v blokaci: natrvalo, ať si bot shání novou
                        _LOGGER.warning("identita odebrána natrvalo: %s", ip[3:11] + "…")
                return True
            return False


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

    def __init__(self, status=200, data=None, location=None, text=None, html=None, utok=None):
        # `utok` = (důvod, otisk nastavení): odmítnutí, které se nepočítá do provozu, ale
        # do přehledu „kdo na nás útočí" (provoz.py). Klientovi se neposílá.
        self.utok = utok
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
TMDB_PREFIX = "tmdb:"


def _imdb_z_tmdb(engine, ctype, tmdb_id):
    """`tmdb:<id>` → `tt…`, prázdno když se přeložit nedá.

    Klienti Stremia (ověřeno u Nuvia 2026-09-18) posílají u titulů z TMDB katalogů
    do `/stream/` id z TMDB. Zdroje ani jádro s ním neumí nic — celý doplněk stojí
    na IMDb id — takže se přeloží přes TMDB. Detail je v jádru cachovaný, takže je
    to jeden dotaz na titul, ne na požadavek.

    **Bez vlastního TMDB klíče** (nepovinné pole formuláře) to nejde: přeložit
    `tmdb:` id umí jen TMDB samo. Pak se vrátí prázdno jako dřív.
    """
    tmdb = getattr(engine, "tmdb", None)
    if tmdb is None:
        _LOGGER.info("tmdb:%s bez klíče TMDB nepřeložím", str(tmdb_id)[:20])
        return ""
    try:
        imdb = tmdb.imdb_id(ctype, tmdb_id)
    except Exception as err:  # noqa: BLE001 – výpadek TMDB = prázdno, ne chyba služby
        _LOGGER.warning("překlad tmdb:%s selhal: %s", str(tmdb_id)[:20], err)
        return ""
    if not imdb:
        _LOGGER.info("tmdb:%s nemá IMDb id", str(tmdb_id)[:20])
    return imdb or ""


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

    def __init__(self, enginy, verze=VERZE, predvyplnit=False, statistiky=None, katalogy=None,
                 blokovane=None, identita=None, blokace=None):
        self.enginy = enginy
        self.identita = identita or Identita("")
        self.id_okno = Okno(*ID_LIMIT)
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
        self.stream_okno = Okno(*STREAM_LIMIT)
        self.play_okno = Okno(*PLAY_LIMIT)
        self.katalog_okno = Okno(*KATALOG_LIMIT)
        self.ip_okno = Okno(*IP_STROP)   # strop na adresu nad limity na uživatele (identitu)
        self.blokace = blokace or Blokace()
        # ruční blokace zneužívající adresy (otisk `config.fingerprint()`, ne účty
        # samotné) — `NOKTURNO_BLOCKED_FINGERPRINTS` v `.env`, viz `server.py`.
        # Incident 2026-09-19: jedna adresa systematicky procházela celý katalog
        # (stream požadavek na tisíce id po řadě) a nafoukla cache adresáře na
        # tolik souborů, že LXC 124 došly inody i sousednímu dashboardu.
        self.blokovane = frozenset(blokovane or ())

    # --- adresy -----------------------------------------------------------
    @staticmethod
    def _rozdel(cesta):
        """`/c/<nastavení>/zbytek` → (nastavení, `/zbytek`); jinak (None, cesta)."""
        casti = [c for c in cesta.split("/") if c]
        if len(casti) >= 2 and casti[0] == "c":
            return casti[1], "/" + "/".join(casti[2:])
        return None, cesta

    def ma_identitu(self, cesta):
        """Čím je adresa jedinečná — jen pro přehled útočníků, bez ověření podpisu:
        1 = token identity (`id`), 2 = vlastní účty (jedinečný otisk), 0 = nic (sdílené)."""
        kousek, _ = self._rozdel(urllib.parse.unquote((cesta or "").split("?", 1)[0]))
        options = config.decode(kousek) if kousek else None
        if not options:
            return 0
        return 1 if options.get(config.ID_KLIC) else 2 if config.ma_ucty(options) else 0

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
    def manifest(self, options, nastaveno, nova_adresa=None):
        """Jen z nastavení — jádro se kvůli manifestu nezakládá (viz `sources_from_options`)."""
        zdroje = config.sources_from_options(options)
        katalogy = self.katalogy.manifest(options) if self.katalogy else []
        data = mapping.manifest(self.verze, zdroje, nastaveno=bool(zdroje), katalogy=katalogy, nova_adresa=nova_adresa)
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

    def configure(self, kousek, zaklad, verejny=False, jazyk="cs", klient=""):
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
        html = html.replace("__ID__", self._identita_pro_formular(soucasne, klient))
        return Odpoved(html=html)

    def _identita_pro_formular(self, soucasne, klient):
        """Token do adresy: stávající platný (v druhé půlce platnosti tiše vyměněný za čerstvý —
        držitel platného už práci prokázal); bez něj si stránka novou vyžádá až za důkaz práce."""
        t = (soucasne or {}).get(config.ID_KLIC)
        if self.identita.zapnuta and t and self.identita.platna(t) and not self.blokace.odebrana("id:" + t):
            return self.identita.vydat() if self.identita.k_obnove(t) else t
        return ""

    def vydat_identitu(self, dotaz, klient):
        """Nová identita za spočítanou výzvu (`identita.over_dukaz`), nejvýš `ID_LIMIT` na adresu."""
        if not self.identita.zapnuta:
            return chyba(404, "Identity se nevydávají.")
        vyzva = (dotaz.get("vyzva") or [""])[0]
        reseni = (dotaz.get("reseni") or [""])[0]
        if not self.identita.over_dukaz(vyzva, reseni, klic_klienta(klient)):
            odp = chyba(403, "Výzva nesedí nebo vypršela.")
            odp.utok = ("špatný důkaz", None)
            return odp
        # limit až za správný důkaz — špatný stojí jen toho, kdo ho poslal
        adresa = klic_klienta(klient)
        if adresa and not self.id_okno.povolit(adresa):
            _LOGGER.info("identita: limit vydávání pro %s", adresa)
            return chyba(429, "Příliš mnoho identit z jedné adresy za hodinu.")
        return Odpoved(data={"id": self.identita.vydat()})

    def _klic_limitu(self, options, klient):
        """Na koho se počítají limity, blokace a nová jádra: identita z adresy, jinak adresa."""
        if options and options.get(config.ID_KLIC):
            return "id:" + options[config.ID_KLIC]
        if options and config.ma_ucty(options):
            return "fp:" + config.fingerprint(options)   # účty v adrese = jedinečný otisk uživatele
        return klic_klienta(klient)

    def _omezit(self, okno, options, klient, co):
        """Limit `okno` na klíč (identita, jinak adresa) + strop `IP_STROP` na adresu, který
        identita neobejde; blokovaná adresa/identita dostane 403. None = smí dál."""
        fp = config.fingerprint(options)
        adresa = self._klic_limitu(options, klient)
        ip = klic_klienta(klient)
        for klic in {adresa, ip} - {""}:
            if self.blokace.blokovana(klic):
                odp = chyba(403, "Tvoje adresa je kvůli množství požadavků na hodinu zablokovaná.")
                odp.utok = ("auto-blok", fp)
                return odp
        vlastni_ok = okno.povolit(adresa or fp)
        ip_ok = not (ip and ip != adresa) or self.ip_okno.povolit(ip)
        if not vlastni_ok or not ip_ok:
            # prohřešek dostane jen ten klíč, který limit opravdu překročil — jinak by jeden
            # uživatel s vlastním otiskem přivedl na blokaci celou domácnost za stejnou IP
            if adresa and not vlastni_ok:
                self.blokace.prohresek(adresa)
            if ip and ip != adresa and not ip_ok:
                self.blokace.prohresek(ip)
            odp = chyba(429, f"Příliš mnoho požadavků na {co} za sebou, zkus to za pár minut.")
            odp.utok = ("limit", fp)
            return odp
        return None

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
        base_id, season, episode = split_episode_id(item_id)
        if base_id.startswith(TMDB_PREFIX):
            base_id = _imdb_z_tmdb(engine, ctype, base_id[len(TMDB_PREFIX):])
            if not base_id:
                return Odpoved(data={"streams": []})
            item_id = base_id if season is None else f"{base_id}:{season}:{episode}"
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
        try:
            skip = max(0, int((urllib.parse.parse_qs(extra).get("skip") or ["0"])[0]))
        except ValueError:
            skip = 0
        if skip > MAX_SKIP:
            # každá hodnota skip je vlastní cache klíč a dotaz na zdroj; hlouběji Stremio nikdo neroluje
            return Odpoved(data={"metas": []})
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
        if cesta == "/identita/vyzva":
            return Odpoved(data={"vyzva": self.identita.vyzva(klic_klienta(klient)), "bity": self.identita.bity})
        if cesta == "/identita":
            return self.vydat_identitu(urllib.parse.parse_qs(dotaz), klient)

        kousek, zbytek = self._rozdel(cesta)
        options = config.decode(kousek) if kousek else None
        if kousek and options is None:
            return chyba(404, "Adresa nese nečitelné nastavení. Vyrob si novou na /configure")
        if options and options.get(config.ID_KLIC):
            platna = self.identita.platna(options[config.ID_KLIC])
            odebrana = self.blokace.odebrana("id:" + options[config.ID_KLIC])
            if not self.identita.zapnuta or (zbytek == "/configure" and (not platna or odebrana)):
                # bez tajemství nejde ověřit; na formuláři se neplatná/odebraná jen zahodí a vydá se nová
                options.pop(config.ID_KLIC)
            elif not platna:
                odp = chyba(403, "Adresa nese neplatnou identitu. Vyrob si novou na /configure")
                odp.utok = ("neplatné id", config.fingerprint(options))
                return odp
            elif odebrana:
                odp = chyba(403, "Tahle identita byla kvůli opakovanému zneužití odebrána. Vyrob si novou na /configure")
                odp.utok = ("odebráno", config.fingerprint(options))
                return odp
        if kousek and self.blokovane and config.fingerprint(options) in self.blokovane:
            odp = chyba(403, "Tahle adresa doplňku je zablokovaná.")
            odp.utok = ("blokováno", config.fingerprint(options))
            return odp
        if verejny and options:
            options = config.bez_lokalnich_uloziste(options)

        if zbytek in ("", "/", "/configure", "/configure/"):
            if zbytek in ("/configure", "/configure/"):
                return self.configure(kousek, zaklad, verejny, jazyk, klient)
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
            if not self.check_okno.povolit(klic_klienta(klient) or "?"):
                odp = chyba(429, "Příliš mnoho ověření za sebou, zkus to za pár minut.")
                odp.utok = ("limit", config.fingerprint(options) if kousek else None)
                return odp
            return self.check(options if kousek else self.enginy.vychozi_options, verejny=verejny)
        # adresa z doby před identitou (do 6.1.0) a bez účtů (jen HellSpy): funguje dál, ale limity
        # sdílí celá IP — uživatele postrčíme na novou (popis doplňku, první položka streamů)
        stara = bool(kousek) and self.identita.zapnuta and not options.get(config.ID_KLIC) and not config.ma_ucty(options)
        nova = f"{zaklad}/c/{kousek}/configure"   # formulář s jeho nastavením, vydá i identitu
        if zbytek == "/manifest.json":
            return self.manifest(options if kousek else self.enginy.vychozi_options, nastaveno=bool(kousek),
                                 nova_adresa=nova if stara else None)

        casti = [c for c in zbytek.split("/") if c]
        if casti and casti[0] == "catalog":
            # katalog na účtech nezávisí — jádro se nezakládá, cache je jedna pro všechny
            if not self.katalog_okno.povolit(klic_klienta(klient) or "?"):
                odp = chyba(429, "Příliš mnoho požadavků na katalog za sebou, zkus to za pár minut.")
                odp.utok = ("limit", config.fingerprint(options) if kousek else None)
                return odp
            return self.katalog(casti)

        if kousek and casti and casti[0] == "stream":
            odp = self._omezit(self.stream_okno, options, klient, "streamy")
            if odp is not None:
                return odp
        if kousek and casti and casti[0] == "play":
            odp = self._omezit(self.play_okno, options, klient, "přehrání")
            if odp is not None:
                return odp
        try:
            engine = self.enginy.pro(options, verejny=verejny, klient=self._klic_limitu(options, klient) if kousek else "")
        except PrilisMnohoNovych:
            adresa = self._klic_limitu(options, klient)
            if adresa:
                self.blokace.prohresek(adresa)
            odp = chyba(429, "Příliš mnoho nových nastavení z jedné adresy za hodinu, zkus to později.")
            odp.utok = ("limit", config.fingerprint(options))
            return odp

        if casti and casti[0] == "play" and len(casti) == 2:
            return self.play(engine, casti[1], klic=config.fingerprint(options) if kousek else "vychozi")
        if casti and casti[0] == "stream" and len(casti) == 3 and casti[2].endswith(".json"):
            odp = self.streams(engine, casti[1], casti[2][:-len(".json")], zaklad, kousek, aplikace)
            if stara and isinstance(odp.data, dict) and isinstance(odp.data.get("streams"), list):
                odp.data["streams"].insert(0, mapping.upozorneni_nova_adresa(nova))
            if kousek and isinstance(odp.data, dict) and odp.data.get("streams"):
                povysit = getattr(self.enginy, "povysit", None)
                if povysit is not None:
                    povysit(options, verejny)   # první skutečný stream = jádro se ověřilo
            return odp
        return chyba(404, "Tady nic není. Doplněk se nastavuje na /configure")
