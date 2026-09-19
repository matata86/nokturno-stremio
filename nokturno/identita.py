"""Identita uživatele v adrese doplňku — vlastní token Nokturna, ne účet u zdroje.

Nastavení jen s HellSpy (bez účtů) má každý stejné, tedy i stejný otisk: jedno jádro,
jeden limit a jedna blokace pro všechny. Bot na takovém nastavení vyčerpal HellSpy všem.
Formulář na `/configure` proto do adresy vloží podepsaný token (`id`), takže má každý
uživatel vlastní otisk — limity, jádra i blokace platí na něj, ne na sdílené nastavení.

Token = `<náhodných 16 hex>.<HMAC-SHA256 tajemstvím, 16 hex>`. Server si nic neukládá
(nic k úniku, žádná tabulka), pravost ověří podpisem. Vydání se omezuje na adresu
(viz `routes.ID_LIMIT`), aby si bot nevygeneroval tisíce. Bez `NOKTURNO_ID_SECRET`
se tokeny nevydávají a v adresách se ignorují — staré adresy bez tokenu fungují dál.
"""
import hmac
import hashlib
import os
import re
import secrets
import time

TVAR = re.compile(r"^[0-9a-f]{16}\.[0-9a-f]{16}$")
VYZVA_RE = re.compile(r"^\d{1,12}\.[0-9a-f]{16}\.[0-9a-f]{16}$")
RESENI_RE = re.compile(r"^[0-9a-zA-Z]{1,32}$")
DUKAZ_BITY = 19          # nulových bitů na začátku SHA-256(výzva + "." + řešení); ~0,5 M pokusů
VYZVA_PLATNOST = 10 * 60


class Identita:
    def __init__(self, tajemstvi=""):
        self._klic = (tajemstvi or "").encode("utf-8")

    @classmethod
    def z_prostredi(cls, environ=None):
        env = os.environ if environ is None else environ
        i = cls(str(env.get("NOKTURNO_ID_SECRET", "")).strip())
        try:
            i.bity = max(0, min(28, int(env.get("NOKTURNO_ID_DUKAZ_BITY", DUKAZ_BITY))))
        except ValueError:
            pass
        return i

    bity = DUKAZ_BITY

    @property
    def zapnuta(self):
        return bool(self._klic)

    def _podpis(self, nahoda):
        return hmac.new(self._klic, nahoda.encode("ascii"), hashlib.sha256).hexdigest()[:16]

    def vydat(self):
        if not self.zapnuta:
            return ""
        nahoda = secrets.token_hex(8)
        return f"{nahoda}.{self._podpis(nahoda)}"

    def platna(self, token):
        """Pravý token od téhle instance? Bez tajemství vždy False."""
        if not self.zapnuta or not isinstance(token, str) or not TVAR.match(token):
            return False
        nahoda, podpis = token.split(".", 1)
        return hmac.compare_digest(self._podpis(nahoda), podpis)

    # --- důkaz práce: identitu vydáme až za spočítanou výzvu (bez třetí strany) ---
    def vyzva(self, now=None):
        """Podepsaná výzva `<čas>.<náhoda>.<podpis>` — bez uloženého stavu, platí 10 minut."""
        if not self.zapnuta:
            return ""
        cas = str(int(now if now is not None else time.time()))
        nahoda = secrets.token_hex(8)
        return f"{cas}.{nahoda}.{self._podpis(cas + '.' + nahoda)}"

    def over_dukaz(self, vyzva, reseni, now=None):
        """Výzva od nás, čerstvá, a SHA-256(výzva + "." + řešení) začíná `bity` nulami."""
        if not self.zapnuta or not isinstance(vyzva, str) or not isinstance(reseni, str):
            return False
        if not VYZVA_RE.match(vyzva) or not RESENI_RE.match(reseni):
            return False
        cas, nahoda, podpis = vyzva.split(".")
        if not hmac.compare_digest(self._podpis(cas + "." + nahoda), podpis):
            return False
        ted = now if now is not None else time.time()
        if not (0 <= ted - int(cas) <= VYZVA_PLATNOST):
            return False
        otisk = hashlib.sha256(f"{vyzva}.{reseni}".encode("ascii")).digest()
        return int.from_bytes(otisk[:4], "big") >> (32 - self.bity) == 0 if self.bity else True


def najdi_reseni(vyzva, bity):
    """Jen pro testy a měření — totéž, co dělá prohlížeč."""
    n = 0
    while True:
        r = str(n)
        if int.from_bytes(hashlib.sha256(f"{vyzva}.{r}".encode()).digest()[:4], "big") >> (32 - bity) == 0:
            return r
        n += 1

