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

TVAR = re.compile(r"^[0-9a-f]{16}\.[0-9a-f]{16}$")


class Identita:
    def __init__(self, tajemstvi=""):
        self._klic = (tajemstvi or "").encode("utf-8")

    @classmethod
    def z_prostredi(cls, environ=None):
        env = os.environ if environ is None else environ
        return cls(str(env.get("NOKTURNO_ID_SECRET", "")).strip())

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
