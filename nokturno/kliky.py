"""Komu už se zpráva z dashboardu nemá ukazovat, protože na ni klikl.

Klik přichází z **prohlížeče** na `/z/<id>/<značka>`, ne z přehrávače, takže v něm
není `/c/<nastavení>` a server by uživatele nepoznal. Celou adresu doplňku tam dát
nejde — nese účty a je fakticky heslo (viz `CLAUDE.md`), a skončila by v historii
prohlížeče i v odkazech, které si lidé posílají.

Do odkazu jde proto jen **značka**: `sha256(tajemství + klíč)` zkrácená na 16 znaků.
Zpátky z ní klíč nejde odvodit a k ničemu jinému než k téhle jedné věci se nedá
použít — není to přihlášení, jen pseudonym. Porovnává se vždy značka se značkou.

Záznamy jsou v souboru vedle ostatních dat, takže restart doplňku zprávu nikomu
nevrátí. Řádek je `<id zprávy>:<značka>`, tedy asi 20 bajtů; nad `MAX_ZAZNAMU`
se nechají jen ty, které patří ještě aktivním zprávám (id se nikdy neopakují,
takže staré řádky jsou jen balast).
"""
import hashlib
import logging
import os
import threading

_LOGGER = logging.getLogger(__name__)

MAX_ZAZNAMU = 200_000


class Kliky:
    """Dvojice (id zprávy, značka uživatele) — kdo na kterou zprávu klikl."""

    def __init__(self, soubor="", tajemstvi="", na_aktivni=None, max_zaznamu=MAX_ZAZNAMU):
        self.soubor = soubor
        self.tajemstvi = tajemstvi or ""
        self.na_aktivni = na_aktivni   # volatelná → množina id zpráv, které se ještě ukazují
        self.max_zaznamu = max_zaznamu
        self._zamek = threading.Lock()
        self._data = set()
        if soubor:
            try:
                with open(soubor, encoding="utf-8") as f:
                    for radek in f:
                        radek = radek.strip()
                        if ":" in radek:
                            id_zpravy, _, znacka = radek.partition(":")
                            if id_zpravy.isdigit() and znacka:
                                self._data.add((int(id_zpravy), znacka))
            except OSError:
                pass

    def znacka(self, klic):
        """Klíč uživatele (`id:`/`fp:`) → značka do odkazu. Prázdný klíč = bez značky,
        takový uživatel se nepozná a zpráva se mu skrývat nebude."""
        if not klic:
            return ""
        return hashlib.sha256((self.tajemstvi + "|" + klic).encode("utf-8")).hexdigest()[:16]

    def videl(self, id_zpravy, klic):
        """Klikl už tenhle uživatel na tuhle zprávu?"""
        znacka = self.znacka(klic)
        return bool(znacka) and (id_zpravy, znacka) in self._data

    def oznac(self, id_zpravy, znacka):
        """Zapamatuj si klik. `znacka` chodí rovnou z adresy, klíč server v té chvíli nemá."""
        if not znacka or not isinstance(id_zpravy, int):
            return
        with self._zamek:
            if (id_zpravy, znacka) in self._data:
                return   # druhý klik nic nemění a nic se nezapisuje
            if len(self._data) >= self.max_zaznamu:
                self._uklid()
                if len(self._data) >= self.max_zaznamu:
                    return
            self._data.add((id_zpravy, znacka))
            self._zapis(f"{id_zpravy}:{znacka}\n")

    def _uklid(self):
        """Nad strop: nechat jen záznamy ještě aktivních zpráv a soubor přepsat.
        Bez seznamu aktivních zpráv se nemaže nic — radši přestat zapisovat než
        vrátit zprávu lidem, kteří ji odklikli."""
        if not callable(self.na_aktivni):
            return
        aktivni = set(self.na_aktivni() or ())
        if not aktivni:
            return
        self._data = {z for z in self._data if z[0] in aktivni}
        self._zapis("".join(f"{i}:{z}\n" for i, z in sorted(self._data)), prepsat=True)

    def _zapis(self, text, prepsat=False):
        if not self.soubor:
            return
        try:
            with open(self.soubor, "w" if prepsat else "a", encoding="utf-8") as f:
                f.write(text)
        except OSError as err:
            _LOGGER.debug("kliky na zprávy se neuložily: %s", err)

    def __len__(self):
        return len(self._data)


def z_prostredi(data_dir, environ=None):
    env = os.environ if environ is None else environ
    return Kliky(soubor=os.path.join(data_dir, "kliky_zprav.txt"),
                 tajemstvi=str(env.get("NOKTURNO_ID_SECRET", "")).strip())
