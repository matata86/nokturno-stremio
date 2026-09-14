"""Síť pro požadavky z internetu: kam se veřejná instance smí připojit.

Adresu úložiště zadává kdokoli, kdo si doplněk přidá, a doplněk ji pak prochází
a soubory z ní přes sebe streamuje. Kontrola jména v nastavení nestačí: cizí server
může odpovědět přesměrováním na `http://127.0.0.1:8080/`, nebo DNS vrátí poprvé
veřejnou adresu a podruhé `127.0.0.1` (rebinding). Hlídá se proto až adresa,
na kterou se skutečně navázalo spojení — u každého hopu, včetně přesměrování.

Z internetu nemá smysl pouštět ani domácí síť a tailnet (100.64/10): uživatel
zvenku na LAN LXC 124 stejně nedosáhne, takže by přes doplněk sahal jen na naše
služby (CoreELEC, Home Assistant, dashboard). Domácí požadavek (viz
`server.je_verejny`) tenhle opener nepoužívá — kvůli němu úložiště existuje.
"""
import http.client
import ipaddress
import socket
import urllib.request

CGNAT = ipaddress.ip_network("100.64.0.0/10")   # tailnet; `is_private` ho nezná


def zakazana(adresa):
    """Je tahle IP mimo veřejný internet? Na ni se z veřejného požadavku nesahá."""
    try:
        ip = ipaddress.ip_address(str(adresa).split("%")[0])
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
            or ip.is_private or ip.is_reserved or (ip.version == 4 and ip in CGNAT))


class ChybaCile(OSError):
    """Spojení vedlo na adresu, na kterou se z internetu nesmí."""


class _HlidaneSpojeni:
    """Po navázání spojení ověří skutečnou adresu protistrany."""

    def connect(self):
        super().connect()
        adresa = self.sock.getpeername()[0]
        if zakazana(adresa):
            self.sock.close()
            raise ChybaCile(f"adresa {adresa} není z internetu povolená")


class HlidaneHTTP(_HlidaneSpojeni, http.client.HTTPConnection):
    pass


class HlidaneHTTPS(_HlidaneSpojeni, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(HlidaneHTTP, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(HlidaneHTTPS, req, context=self._context)


class _JenHttp(urllib.request.HTTPRedirectHandler):
    """Přesměrování jen na http(s) — na ftp:// by hlídané spojení nešlo."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith(("http://", "https://")):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verejny_opener():
    """Opener pro požadavky z internetu — každé spojení projde `zakazana()`."""
    return urllib.request.build_opener(_JenHttp, _HTTPHandler, _HTTPSHandler)


OPENER = verejny_opener()


def resolvuj(host):
    """Adresy, na které se jméno překládá — pro rychlé odmítnutí už v nastavení."""
    return [ai[4][0] for ai in socket.getaddrinfo(host, None)]
