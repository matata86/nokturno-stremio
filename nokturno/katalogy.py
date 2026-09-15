"""Volitelné katalogy pro Stremio — stejné seznamy jako v doplňku pro Kodi, bez žánrů a roků.

Každý katalog si uživatel zapne zvlášť ve formuláři (volba `katalogy`, klíče oddělené
čárkou). Výpis na účtech nezávisí, a proto je **jeden pro všechny adresy doplňku**:
vlastní `Store` ve složce `katalogy`, platnost 6 h. Server se tak Sosáče i TMDB ptá
nanejvýš jednou za 6 hodin na katalog a stránku, ať doplněk používá kolik lidí chce —
to byla obava, kvůli které byly katalogy dřív zamítnuté (dokumentace, fáze 3).

Položky nesou jen IMDb id (`tt…`). Detail titulu i díly seriálu si pak Stremio vezme
z Cinemety a streamy od Nokturna jako u každého jiného titulu, takže doplněk nemusí
dodávat metadata. Titul bez IMDb id se vynechá (u Sosáče výjimečně čerstvé přírůstky).

TMDB potřebuje API klíč. Ve formuláři ho záměrně nemáme, bere se klíč instance
z prostředí (`NOKTURNO_TMDB_KEY`); bez něj se katalogy TMDB nenabídnou.
"""
import logging
import os

from .core.lib.sosac_direct import SosacDirect
from .core.lib.store import Store
from .core.lib.tmdb_api import TmdbApi
from .core.lib.trend_api import CATALOG_ID as TREND_CATALOG_ID, TrendApi

_LOGGER = logging.getLogger(__name__)

TTL = 6 * 3600
PREFIX = "nokturno."
STRANKA_SOSAC = 100   # Sosáč vydává dlouhé seznamy, TMDB stránkuje po 20 samo
TMDB_IMG = "https://image.tmdb.org/"
METAHUB_POSTER = "https://images.metahub.space/poster/medium/{}/img"

# klíč, typ, zdroj, id katalogu ve zdroji, název česky, název slovensky
SEZNAM = (
    ("trend.nejsledovanejsi.filmy", "movie", "trend", TREND_CATALOG_ID,
     "Nejsledovanější tento týden", "Najsledovanejšie tento týždeň"),
    ("trend.nejsledovanejsi.serialy", "series", "trend", TREND_CATALOG_ID,
     "Nejsledovanější tento týden", "Najsledovanejšie tento týždeň"),
    ("sosac.popularni.filmy", "movie", "sosac", "moviesmostpopular",
     "Nejpopulárnější filmy", "Najpopulárnejšie filmy"),
    ("sosac.nove.filmy", "movie", "sosac", "moviesrecentlyadded",
     "Nově přidané filmy", "Novo pridané filmy"),
    ("sosac.nove.dabing", "movie", "sosac", "moviesrecentlyadded_dub",
     "Nově přidané s CZ dabingem", "Novo pridané s CZ dabingom"),
    ("sosac.nove.titulky", "movie", "sosac", "moviesrecentlyadded_subs",
     "Nově přidané s CZ titulky", "Novo pridané s CZ titulkami"),
    ("sosac.popularni.serialy", "series", "sosac", "tvshowsmostpopular",
     "Nejpopulárnější seriály", "Najpopulárnejšie seriály"),
    ("tmdb.trendy.filmy", "movie", "tmdb", "trending", "Trendy filmy tento týden", "Trendy filmy tento týždeň"),
    ("tmdb.popularni.filmy", "movie", "tmdb", "popular", "Populární filmy", "Populárne filmy"),
    ("tmdb.nejlepsi.filmy", "movie", "tmdb", "top_rated", "Nejlépe hodnocené filmy", "Najlepšie hodnotené filmy"),
    ("tmdb.trendy.serialy", "series", "tmdb", "trending", "Trendy seriály tento týden", "Trendy seriály tento týždeň"),
    ("tmdb.popularni.serialy", "series", "tmdb", "popular", "Populární seriály", "Populárne seriály"),
    ("tmdb.nejlepsi.serialy", "series", "tmdb", "top_rated", "Nejlépe hodnocené seriály", "Najlepšie hodnotené seriály"),
)

# Nabídka ve formuláři (2026-09-15) sjednocená s menu Filmy/Seriály v Kodi — „Populární na
# TMDB“, „Nejsledovanější tento týden“, „Nejlépe hodnocené“, „Nově přidané s CZ dabingem/titulky“
# (ty poslední dvě fakt jen ze Sosáčova vlastního značení „d“/„s“, žádné živé ověřování napříč
# zdroji jako v Kodi — to by tady muselo běžet pro každého uživatele katalogu zvlášť, ne jednou
# za 6 h sdíleně). `SEZNAM` výš zůstává beze změny (i staré řádky níž) — kdo je má už zapnuté
# v uloženém nastavení, dál mu fungují, jen se nový uživatel k nim ve formuláři nedostane.
DOPORUCENE = {
    "trend.nejsledovanejsi.filmy", "trend.nejsledovanejsi.serialy",
    "tmdb.popularni.filmy", "tmdb.popularni.serialy",
    "tmdb.nejlepsi.filmy", "tmdb.nejlepsi.serialy",
    "sosac.nove.dabing", "sosac.nove.titulky",
}


def nahled(typ, meta):
    """Metadata ze zdroje → `metaPreview` Stremia. Bez IMDb id None."""
    imdb = str(meta.get("imdb_id") or meta.get("id") or "")
    if not imdb.startswith("tt"):
        return None
    out = {"id": imdb, "type": typ, "name": meta.get("name") or "", "posterShape": "poster"}
    # obrázky Sosáče (movies.sosac.tv) mimo jeho web přesměrují na stránku a jsou na šířku —
    # plakát proto z TMDB, jinak podle IMDb id z metahubu (ten používá i Cinemeta)
    poster = str(meta.get("poster") or "")
    out["poster"] = poster if poster.startswith(TMDB_IMG) else METAHUB_POSTER.format(imdb)
    if str(meta.get("background") or "").startswith(TMDB_IMG):
        out["background"] = meta["background"]
    if meta.get("description"):
        out["description"] = meta["description"]
    rok = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    if rok:
        out["releaseInfo"] = rok
    if meta.get("genres"):
        out["genres"] = list(meta["genres"])[:5]
    try:
        if meta.get("imdbRating"):
            out["imdbRating"] = f"{float(meta['imdbRating']):.1f}"
    except (TypeError, ValueError):
        pass
    return out


class Katalogy:
    def __init__(self, data_dir, tmdb_key="", ttl=TTL):
        self.store = Store(os.path.join(data_dir, "katalogy"))
        self.ttl = ttl
        self.sosac = SosacDirect(cache=self.store, index_store=self.store.index())
        self.tmdb = TmdbApi(tmdb_key, cache=self.store) if str(tmdb_key or "").strip() else None
        self.trend = TrendApi(cache=self.store)

    def dostupne(self):
        return [radek for radek in SEZNAM if radek[2] != "tmdb" or self.tmdb is not None]

    def formular(self, jazyk="cs"):
        """Nabídka pro formulář — jen doporučené katalogy (`DOPORUCENE`), které tahle
        instance umí. Starší katalogy z `SEZNAM` (mimo `DOPORUCENE`) se ve formuláři
        novým uživatelům nenabízejí, ale `dostupne()`/`vybrane()`/`manifest()` je
        pořád umí vyřešit — kdo je má uložené v adrese, nic mu nepřestane fungovat."""
        return [{"klic": klic, "typ": typ, "nazev": sk if jazyk == "sk" else cs}
                for klic, typ, _zdroj, _cid, cs, sk in self.dostupne() if klic in DOPORUCENE]

    def vybrane(self, options):
        chtene = {x.strip() for x in str((options or {}).get("katalogy") or "").split(",") if x.strip()}
        return [radek for radek in self.dostupne() if radek[0] in chtene]

    def manifest(self, options):
        return [{"type": typ, "id": PREFIX + klic, "name": cs, "extra": [{"name": "skip", "isRequired": False}]}
                for klic, typ, _zdroj, _cid, cs, _sk in self.vybrane(options)]

    def polozky(self, typ, katalog_id, skip=0):
        """Náhledy jedné stránky katalogu. None = takový katalog tahle instance nemá."""
        klic = katalog_id[len(PREFIX):] if str(katalog_id).startswith(PREFIX) else ""
        radek = next((r for r in self.dostupne() if r[0] == klic and r[1] == typ), None)
        if radek is None:
            return None
        try:
            skip = max(0, int(skip or 0))
        except (TypeError, ValueError):
            skip = 0
        _klic, typ, zdroj, cid = radek[:4]

        def load():
            if zdroj == "sosac":
                raw = self.sosac.catalog(typ, cid, skip=skip, page=STRANKA_SOSAC)
            elif zdroj == "trend":
                # vlastní žebříček dashboardu — nejvýš 50 položek, `TrendApi.catalog()`
                # sám vrátí prázdno pro skip > 0 (stránkování nemá co nabídnout)
                raw = self.trend.catalog(typ, cid, skip=skip)
            else:
                raw = self.tmdb.catalog(typ, cid, skip=skip)
            return [p for p in (nahled(typ, m) for m in raw or []) if p]
        try:
            # prázdný výsledek (výpadek zdroje) se nepamatuje — další dotaz zkusí znovu
            return self.store.cached_if(f"stremio:katalog:{klic}:{skip}", self.ttl, load) or []
        except Exception as err:  # noqa: BLE001 – výpadek zdroje = prázdný katalog, ne chyba služby
            _LOGGER.warning("katalog %s (skip %d): %s", klic, skip, err)
            return []
