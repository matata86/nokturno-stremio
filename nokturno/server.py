"""HTTP vrstva doplňku.

Stojí na `http.server` ze standardní knihovny, protože jádro je taky bez
závislostí a volání v něm jsou blokující. `ThreadingHTTPServer` obslouží každý
požadavek ve vlákně, takže dlouhé hledání na WebShare nezastaví ostatní dotazy —
stejný vzor, jakým dnes integrace pro Home Assistant pouští jádro v executoru.

    python3 -m nokturno.server --port 7127
"""
import argparse
import logging
import os
import re
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import decode, fingerprint, from_environ, sources_summary
from .katalogy import Katalogy
from .enginy import Enginy
from .routes import VERZE, Router, jazyk_z_hlavicky, klient_z_useragent
from .statistiky import Statistiky
from .pady import Pady
from .provoz import Provoz

_LOGGER = logging.getLogger("nokturno")

VYCHOZI_PORT = 7127          # hned vedle Luny na 7126
VYCHOZI_DATA = "./data"      # cache a mezipaměť jádra; v kontejneru svazek


def je_verejny(headers, client_ip):
    """Přišel požadavek z internetu přes Tailscale Funnel?

    Veřejný požadavek nesmí dostat výchozí nastavení z prostředí — tedy účty
    WebShare a Streamuj majitele instance. Rozhoduje se tak, aby pochybnost
    znamenala „veřejný":

    - `Tailscale-Funnel-Request` přidává Funnel ke každému požadavku z internetu;
    - `Tailscale-User-Login` přidává `tailscale serve` jen přihlášenému uživateli
      tailnetu. Funnel tyhle hlavičky od klienta zahodí, podvrhnout nejdou
      (ověřeno 2026-09-13 zvenku s ručně poslanou hlavičkou);
    - přímý přístup mimo proxy Tailscale (LAN na :7127) je soukromý jako dřív,
      ale bez identity přes proxy (tagované zařízení, cokoli nečekaného) už ne.
    """
    if headers.get("Tailscale-Funnel-Request"):
        return True
    if headers.get("Tailscale-User-Login"):
        return False
    return client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def bezpecna_cesta(path):
    """Cesta do logu: `/c/<účty>/…` → `/c/<otisk>/…`. Adresa doplňku je fakticky heslo
    (viz CLAUDE.md), do journalu nepatří ani při chybě."""
    def otisk(m):
        options = decode(m.group(1))
        return "/c/" + (fingerprint(options) if options else "?")
    return re.sub(r"^/c/([^/?]+)", otisk, path or "")


def uklid_dat(data_dir, max_age_s=30 * 86400):
    """Složky jader, na které se 30 dní nesáhlo — každá adresa doplňku má vlastní,
    a ty s překlepem nebo od zkoušejících by jinak zůstaly navždy."""
    hranice = time.time() - max_age_s
    smazano = 0
    try:
        for name in os.listdir(data_dir):
            path = os.path.join(data_dir, name)
            if os.path.isdir(path) and re.fullmatch(r"[0-9a-f]{16}", name) and os.path.getmtime(path) < hranice:
                shutil.rmtree(path, ignore_errors=True)
                smazano += 1
    except OSError:
        pass
    return smazano


class Handler(BaseHTTPRequestHandler):
    server_version = f"nokturno/{VERZE}"
    sys_version = ""                 # verze Pythonu do hlavičky Server nepatří
    protocol_version = "HTTP/1.1"    # Stremio drží spojení otevřené
    timeout = 60                     # nečinné spojení nesmí držet vlákno navždy
    _verejny = True                  # do_GET přepíše; při pochybnosti veřejný
    # měření provozu; instance handleru žije přes celé keep-alive spojení, takže
    # `_zacni()` je na začátku každé obsluhy, ne v konstruktoru
    _zacatek = 0.0
    _stav = 0
    _zapsano = 0
    _nahlaseno = True

    # hlavičky pro stránky (úvod, formulář): žádné cizí skripty, žádné vkládání do
    # rámu, žádný Referer — formulář sbírá hesla a jeho adresa nese účty
    HLAVICKY_STRANEK = (
        ("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                    "img-src https://raw.githubusercontent.com data:; connect-src 'self'; "
                                    "base-uri 'none'; frame-ancestors 'none'"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
    )

    # --- měření provozu (provoz.py) ---------------------------------------
    def _zacni(self):
        self._zacatek = time.monotonic()
        self._stav = 0
        self._zapsano = 0
        self._nahlaseno = False

    def send_response(self, code, message=None):
        self._stav = code
        super().send_response(code, message)

    def _nahlas(self):
        """Jeden řádek do fronty provozu. Volá se z každé obsluhy v `finally`, ať
        se dostane i na spojení, které klient uprostřed zavřel."""
        provoz = getattr(self.server, "provoz", None)
        if provoz is None or getattr(self, "_nahlaseno", True):
            return
        self._nahlaseno = True
        try:
            provoz.zaznamenej(self.path, self.command or "GET", self._stav or 499, self._zapsano,
                              int((time.monotonic() - self._zacatek) * 1000),
                              klient_z_useragent(self.headers.get("User-Agent")))
        except Exception:  # noqa: BLE001 – statistika provozu nesmí nic shodit
            _LOGGER.debug("provoz se nezaznamenal", exc_info=True)

    # --- pomůcky ----------------------------------------------------------
    def _klient(self):
        """Adresa klienta — přes Tailscale proxy z X-Forwarded-For, jinak peer."""
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
        return peer

    def _zaklad(self):
        """Absolutní adresa, na kterou se klient ptá.

        Bere se z hlavičky požadavku, ne z nastavení: Stremio přehrává na jiném
        zařízení, než kde běží služba, a odkazy na `/play/` mu musí zůstat
        dosažitelné. Hlavičky X-Forwarded-* respektujeme kvůli případné proxy.
        """
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
        if not host:
            host = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        schema = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{schema}://{host}"

    def _posli(self, odpoved):
        telo, typ = odpoved.body
        self.send_response(odpoved.status)
        if odpoved.location:
            self.send_header("Location", odpoved.location)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(telo)))
        # Stremio si doplněk tahá z webového klienta, takže bez CORS by neprošel
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        if odpoved.html is not None:
            self.send_header("Vary", "Accept-Language")   # stránky jsou česky nebo slovensky
            for jmeno, hodnota in self.HLAVICKY_STRANEK:
                self.send_header(jmeno, hodnota)
        if self.path.startswith("/c/"):
            # adresa nese účty — nic z ní nemá zůstat v cache prohlížeče ani proxy
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(telo)
            self._zapsano += len(telo)

    # --- metody -----------------------------------------------------------
    def do_GET(self):
        self._odeslano = False
        self._zacni()
        try:
            verejny = je_verejny(self.headers, self.client_address[0])
            self._verejny = verejny
            jazyk = jazyk_z_hlavicky(self.headers.get("Accept-Language"))
            aplikace = klient_z_useragent(self.headers.get("User-Agent"))
            self._posli(self.server.router.route(self.path, self._zaklad(), verejny=verejny, jazyk=jazyk,
                                                 klient=self._klient(), aplikace=aplikace))
        except (BrokenPipeError, ConnectionResetError):
            # přehrávač si to rozmyslel a zavřel spojení — běžné, ne chyba
            _LOGGER.debug("klient zavřel spojení při %s", bezpecna_cesta(self.path))
        except Exception:  # noqa: BLE001 – žádná chyba nesmí ukončit službu
            _LOGGER.exception("neočekávaná chyba při %s", bezpecna_cesta(self.path))
            pady = getattr(self.server, "pady", None)
            if pady is not None:
                pady.zaznamenej(sys.exc_info()[1], self.path)
            if self._odeslano:
                self.close_connection = True   # hlavičky už odešly — druhá odpověď by rozbila keep-alive
                return
            try:
                # text stavového řádku musí být latin-1 — „Chyba doplňku" tam dřív shodilo
                # odeslání a klient čekal na timeout; česky jde jen do těla odpovědi
                self.send_error(500, "Internal Server Error", "Chyba doplňku")
            except Exception:  # noqa: BLE001 – klient už mohl spojení zavřít
                self.close_connection = True
        finally:
            self._nahlas()

    def do_HEAD(self):
        # HEAD na streamy/ověření dřív spustilo celé hledání ve zdrojích jen kvůli hlavičkám
        if "/stream/" in self.path or self.path.rstrip("/").endswith("/check"):
            self._zacni()
            try:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
            finally:
                self._nahlas()
            return
        self.do_GET()

    def do_OPTIONS(self):
        self._zacni()
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()
        finally:
            self._nahlas()

    def log_message(self, format, *args):  # noqa: A002 – podpis dává BaseHTTPRequestHandler
        _LOGGER.debug("%s %s", self.address_string(), bezpecna_cesta(format % args))


def vytvor_server(host="0.0.0.0", port=VYCHOZI_PORT, data_dir=VYCHOZI_DATA, options=None,
                  predvyplnit=None):
    """Server s připravenými jádry. Nespouští smyčku — to dělá volající.

    Vrácené zdroje jsou ty z prostředí, tedy výchozí konfigurace. Uživatelé
    s vlastní adresou mají svoje a server o nich dopředu neví.
    """
    os.makedirs(data_dir, exist_ok=True)
    smazano = uklid_dat(data_dir)
    if smazano:
        _LOGGER.info("úklid: %d složek jader bez použití přes 30 dní", smazano)
    vychozi = options if options is not None else from_environ()
    enginy = Enginy(data_dir, vychozi)
    zdroje = sources_summary(enginy.pro())
    if predvyplnit is None:
        predvyplnit = os.environ.get("NOKTURNO_CONFIGURE_PREFILL", "").strip().lower() in ("1", "true", "ano", "yes")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    # katalogy sdílí jednu cache pro všechny adresy; TMDB jen s klíčem instance (viz katalogy.py)
    # seriály podle jazyka ověřuje výchozí (domácí) jádro s účty instance, viz katalogy.py
    katalogy = Katalogy(data_dir, os.environ.get("NOKTURNO_TMDB_KEY", ""), engine=enginy.pro)
    if options is None:
        # ať první dotaz po restartu nevrátí prázdný seznam seriálů; jen služba z prostředí (testy jdou bez sítě)
        katalogy.zahrat()
    server.router = Router(enginy, predvyplnit=predvyplnit, statistiky=Statistiky.z_prostredi(VERZE),
                           katalogy=katalogy)
    server.pady = Pady.z_prostredi(data_dir, VERZE)
    server.pady.odesli()   # co zůstalo ve frontě z minula (server nebo síť tehdy neběžely)
    server.provoz = Provoz.z_prostredi()
    server.provoz.start()  # bez NOKTURNO_TRAFFIC_TOKEN se vlákno nespustí a nic se neměří
    return server, zdroje


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nokturno pro Stremio")
    ap.add_argument("--host", default=os.environ.get("NOKTURNO_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("NOKTURNO_PORT", VYCHOZI_PORT)))
    ap.add_argument("--data", default=os.environ.get("NOKTURNO_DATA", VYCHOZI_DATA),
                    help="kam ukládat cache jádra (v kontejneru svazek, jinak se po restartu tahá znovu)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    server, zdroje = vytvor_server(args.host, args.port, args.data)
    _LOGGER.info("Nokturno %s běží na http://%s:%d", VERZE, args.host, args.port)
    _LOGGER.info("zdroje výchozího nastavení: %s", ", ".join(zdroje) if zdroje else "žádné, viz README")
    _LOGGER.info("nastavení a adresa doplňku: http://<adresa tohohle stroje>:%d/configure", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _LOGGER.info("končím")
    finally:
        server.provoz.stop()
        server.provoz.odesli()   # co se nastřádalo od poslední dávky
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
