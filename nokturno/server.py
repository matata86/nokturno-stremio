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
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import from_environ, sources_summary
from .enginy import Enginy
from .routes import VERZE, Router

_LOGGER = logging.getLogger("nokturno")

VYCHOZI_PORT = 7127          # hned vedle Luny na 7126
VYCHOZI_DATA = "./data"      # cache a mezipaměť jádra; v kontejneru svazek


class Handler(BaseHTTPRequestHandler):
    server_version = f"nokturno/{VERZE}"
    protocol_version = "HTTP/1.1"    # Stremio drží spojení otevřené

    # --- pomůcky ----------------------------------------------------------
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
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(telo)

    # --- metody -----------------------------------------------------------
    def do_GET(self):
        try:
            self._posli(self.server.router.route(self.path, self._zaklad()))
        except (BrokenPipeError, ConnectionResetError):
            # přehrávač si to rozmyslel a zavřel spojení — běžné, ne chyba
            _LOGGER.debug("klient zavřel spojení při %s", self.path)
        except Exception:  # noqa: BLE001 – žádná chyba nesmí ukončit službu
            _LOGGER.exception("neočekávaná chyba při %s", self.path)
            try:
                self.send_error(500, "Chyba doplňku")
            except Exception:  # noqa: BLE001 – klient už mohl spojení zavřít
                pass

    do_HEAD = do_GET

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 – podpis dává BaseHTTPRequestHandler
        _LOGGER.debug("%s %s", self.address_string(), format % args)


def vytvor_server(host="0.0.0.0", port=VYCHOZI_PORT, data_dir=VYCHOZI_DATA, options=None,
                  predvyplnit=None):
    """Server s připravenými jádry. Nespouští smyčku — to dělá volající.

    Vrácené zdroje jsou ty z prostředí, tedy výchozí konfigurace. Uživatelé
    s vlastní adresou mají svoje a server o nich dopředu neví.
    """
    os.makedirs(data_dir, exist_ok=True)
    vychozi = options if options is not None else from_environ()
    enginy = Enginy(data_dir, vychozi)
    zdroje = sources_summary(enginy.pro())
    if predvyplnit is None:
        predvyplnit = os.environ.get("NOKTURNO_CONFIGURE_PREFILL", "").strip().lower() in ("1", "true", "ano", "yes")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.router = Router(enginy, predvyplnit=predvyplnit)
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
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
