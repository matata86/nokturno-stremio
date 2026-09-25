"""CZtor ve Stremiu — párování PINem a tokeny zapečetěné klíčem z adresy doplňku.

CZtor se nepřihlašuje jménem a heslem, ale párováním zařízení PINem na
`cztor.com/activate` (jádro `lib/cztor_api`). Obnovovací token se při každém použití
vymění, takže do statické adresy doplňku ho dát nejde — musí ležet na serveru.

Server ho ale nemá umět přečíst sám. Formulář `/configure` při párování dostane
náhodný klíč (128 bitů) a ten jde do adresy doplňku jako pole `cz`. Soubor na serveru
(`data/_cztor/<ident>.bin`) je zapečetěný tímtéž klíčem (`sealbox`, jako synchronizace):
bez adresy uživatele je to šum a ze jména souboru se klíč odvodit nedá. Obnovený token
server zapečetí zpátky během téhož požadavku — klíč má v ruce právě jen tehdy.

Adresa doplňku tak zůstává tím, čím byla vždycky: kdo ji má, má účty. CZtor tím nijak
nevybočuje z WebShare nebo Sledujteto, jen se jeho „heslo" za adresou mění samo.
"""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time

from .core.lib import sealbox
from .core.lib.cztor_api import CztorApi

SLOZKA = "_cztor"                       # vedle složek jader, `server.uklid_dat` ji nesmaže
KLIC_RE = re.compile(r"^[0-9a-f]{32}$")
JMENO_ZARIZENI = "Nokturno Stremio"      # tak se zařízení ukáže v seznamu na cztor.com
MAX_STARI = 90 * 86400                   # nepoužívaný soubor zmizí (token se obnovuje denně, mtime s ním)
_SUL = b"nokturno-cztor-stremio-v1"


def novy_klic():
    return secrets.token_hex(16)


def slozka(data_dir):
    return os.path.join(data_dir, SLOZKA)


class _Klice:
    """Stejné atributy jako `sealbox.Keys`. Klíč je náhodný, ne opsaný z obrazovky,
    takže PBKDF2 netřeba — stačí HMAC."""

    __slots__ = ("ident", "enc", "mac")

    def __init__(self, klic):
        root = hmac.new(_SUL, bytes.fromhex(klic), hashlib.sha256).digest()
        self.ident = hmac.new(root, b"gid", hashlib.sha256).hexdigest()[:32]
        self.enc = hmac.new(root, b"enc", hashlib.sha256).digest()
        self.mac = hmac.new(root, b"mac", hashlib.sha256).digest()


class Trezor:
    """Úložiště pro `CztorApi` (`load`/`save`/`cached`), na disku zapečetěné klíčem.
    `cache` = obyčejný `Store` jádra pro hledání v katalogu — na účtu nezávisí a tajné není."""

    def __init__(self, data_dir, klic, cache=None):
        if not KLIC_RE.match(klic or ""):
            raise ValueError("neplatný klíč CZtor")
        self._klice = _Klice(klic)
        self._cache = cache
        self.cesta = os.path.join(slozka(data_dir), self._klice.ident + ".bin")

    def existuje(self):
        return os.path.exists(self.cesta)

    def _vse(self):
        try:
            with open(self.cesta, "rb") as f:
                blob = f.read()
        except OSError:
            return {}
        return sealbox.unseal(self._klice, blob) or {}

    def load(self, name, default=None):
        return self._vse().get(name, default)

    def save(self, name, data):
        vse = self._vse()
        vse[name] = data
        os.makedirs(os.path.dirname(self.cesta), exist_ok=True)
        tmp = f"{self.cesta}.{threading.get_ident()}.tmp"
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            f.write(sealbox.seal(self._klice, vse))
        os.replace(tmp, self.cesta)

    def cached(self, key, ttl, loader):
        return self._cache.cached(key, ttl, loader) if self._cache is not None else loader()


def klient(data_dir, klic, cache=None):
    return CztorApi(Trezor(data_dir, klic, cache), device_name=JMENO_ZARIZENI)


def uklid(data_dir, max_stari=MAX_STARI):
    """Smaže soubory, na které se `max_stari` nesáhlo. Obsah server nepřečte, takže
    nepoznáme, jestli jde o nedokončené párování — stáří je jediné měřítko."""
    hranice = time.time() - max_stari
    smazano = 0
    try:
        for name in os.listdir(slozka(data_dir)):
            cesta = os.path.join(slozka(data_dir), name)
            try:
                if os.path.getmtime(cesta) < hranice:
                    os.remove(cesta)
                    smazano += 1
            except OSError:
                pass
    except OSError:
        pass
    return smazano
