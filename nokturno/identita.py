"""Identita uživatele v adrese doplňku — vlastní token Nokturna, ne účet u zdroje.

Nastavení jen s HellSpy (bez účtů) má každý stejné, tedy i stejný otisk: jedno jádro,
jeden limit a jedna blokace pro všechny. Bot na takovém nastavení vyčerpal HellSpy všem.
Formulář na `/configure` proto do adresy vloží podepsaný token (`id`), takže má každý
uživatel vlastní otisk — limity, jádra i blokace platí na něj, ne na sdílené nastavení.

Token (od 6.1.3) = `<čas vydání, 8 hex>.<náhodných 16 hex>.<HMAC-SHA256 tajemstvím, 16 hex>`
a platí `PLATNOST` (90 dní); formulář ho v druhé půlce platnosti tiše obnoví. Starší tvar
bez času (`<16 hex>.<16 hex>`, 6.1.0–6.1.2) se bere jako platný do `STARE_DO`, pak musí
uživatel projít formulářem znovu. Bez platnosti by nasbírané identity platily navždy
(audit 2026-09-19). Server si nic neukládá (nic k úniku, žádná tabulka), pravost ověří
podpisem. Vydání se omezuje na adresu (viz `routes.ID_LIMIT`), aby si bot nevygeneroval
tisíce, a jde jen za důkaz práce, jehož výzva je **vázaná na adresu klienta** a použít
ji jde jen jednou. Bez `NOKTURNO_ID_SECRET` se tokeny nevydávají a v adresách se ignorují —
staré adresy bez tokenu fungují dál.
"""
import hmac
import hashlib
import os
import re
import secrets
import threading
import time

TVAR = re.compile(r"^[0-9a-f]{8}\.[0-9a-f]{16}\.[0-9a-f]{16}$")
TVAR_STARY = re.compile(r"^[0-9a-f]{16}\.[0-9a-f]{16}$")
VYZVA_RE = re.compile(r"^\d{1,12}\.[0-9a-f]{16}\.[0-9a-f]{16}$")
RESENI_RE = re.compile(r"^[0-9a-zA-Z]{1,32}$")
DUKAZ_BITY = 19          # nulových bitů na začátku SHA-256(výzva + "." + řešení); ~0,5 M pokusů
VYZVA_PLATNOST = 10 * 60
PLATNOST = 90 * 86400    # platnost tokenu; obnovuje se tiše na /configure
STARE_DO = 1797120000    # 2026-12-13: dokdy platí tokeny bez času vydání (6.1.0–6.1.2)
POUZITYCH_MAX = 20000    # kolik vyřešených výzev si pamatovat proti opakovanému použití


class Identita:
    def __init__(self, tajemstvi=""):
        self._klic = (tajemstvi or "").encode("utf-8")
        self._pouzite = {}          # výzva → čas; víc než `VYZVA_PLATNOST` staré se vyhazují
        self._zamek = threading.Lock()

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

    def _podpis(self, text):
        return hmac.new(self._klic, text.encode("ascii"), hashlib.sha256).hexdigest()[:16]

    def vydat(self, now=None):
        if not self.zapnuta:
            return ""
        cas = f"{int(now if now is not None else time.time()):08x}"
        nahoda = secrets.token_hex(8)
        return f"{cas}.{nahoda}.{self._podpis(cas + '.' + nahoda)}"

    @staticmethod
    def vydana(token):
        """Čas vydání tokenu; None u starého tvaru nebo nesmyslu."""
        if isinstance(token, str) and TVAR.match(token):
            return int(token.split(".", 1)[0], 16)
        return None

    def platna(self, token, now=None):
        """Pravý token od téhle instance a v platnosti? Bez tajemství vždy False."""
        if not self.zapnuta or not isinstance(token, str):
            return False
        ted = now if now is not None else time.time()
        if TVAR.match(token):
            cas, nahoda, podpis = token.split(".")
            if not hmac.compare_digest(self._podpis(cas + "." + nahoda), podpis):
                return False
            vydano = int(cas, 16)
            return vydano <= ted + 60 and ted - vydano <= PLATNOST
        if TVAR_STARY.match(token):
            nahoda, podpis = token.split(".", 1)
            return ted < STARE_DO and hmac.compare_digest(self._podpis(nahoda), podpis)
        return False

    def k_obnove(self, token, now=None):
        """Platný token, který je ale starý (druhá půlka platnosti nebo starý tvar) — formulář
        ho má vyměnit za čerstvý, ať uživateli neexpiruje uprostřed používání."""
        if not self.platna(token, now):
            return False
        vydano = self.vydana(token)
        ted = now if now is not None else time.time()
        return vydano is None or ted - vydano > PLATNOST / 2

    # --- důkaz práce: identitu vydáme až za spočítanou výzvu (bez třetí strany) ---
    def vyzva(self, klient="", now=None):
        """Podepsaná výzva `<čas>.<náhoda>.<podpis>` — bez uloženého stavu, platí 10 minut.
        Podpis kryje i adresu klienta (`klient`), takže vyřešená výzva z jiné adresy neplatí."""
        if not self.zapnuta:
            return ""
        cas = str(int(now if now is not None else time.time()))
        nahoda = secrets.token_hex(8)
        return f"{cas}.{nahoda}.{self._podpis(cas + '.' + nahoda + '.' + (klient or ''))}"

    def over_dukaz(self, vyzva, reseni, klient="", now=None):
        """Výzva od nás pro tohohle klienta, čerstvá, ještě nepoužitá, a SHA-256(výzva + "." +
        řešení) začíná `bity` nulami. Správný důkaz výzvu spotřebuje."""
        if not self.zapnuta or not isinstance(vyzva, str) or not isinstance(reseni, str):
            return False
        if not VYZVA_RE.match(vyzva) or not RESENI_RE.match(reseni):
            return False
        cas, nahoda, podpis = vyzva.split(".")
        if not hmac.compare_digest(self._podpis(cas + "." + nahoda + "." + (klient or "")), podpis):
            return False
        ted = now if now is not None else time.time()
        if not (0 <= ted - int(cas) <= VYZVA_PLATNOST):
            return False
        otisk = hashlib.sha256(f"{vyzva}.{reseni}".encode("ascii")).digest()
        if self.bity and int.from_bytes(otisk[:4], "big") >> (32 - self.bity) != 0:
            return False
        with self._zamek:
            if vyzva in self._pouzite:
                return False
            if len(self._pouzite) >= POUZITYCH_MAX:
                hranice = ted - VYZVA_PLATNOST
                self._pouzite = {v: t for v, t in self._pouzite.items() if t > hranice}
                if len(self._pouzite) >= POUZITYCH_MAX:
                    return False   # pořád plno = někdo výzvy sype; radši nevydat než přetéct
            self._pouzite[vyzva] = ted
        return True


def najdi_reseni(vyzva, bity):
    """Jen pro testy a měření — totéž, co dělá prohlížeč."""
    n = 0
    while True:
        r = str(n)
        if int.from_bytes(hashlib.sha256(f"{vyzva}.{r}".encode()).digest()[:4], "big") >> (32 - bity) == 0:
            return r
        n += 1
