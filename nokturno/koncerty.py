"""Nokturno Koncerty — samostatný doplněk pro Stremio nad katalogem koncertů dashboardu.

Koncert nemá IMDb id, takže nepatří do hlavního doplňku (ten Stremio volá s id titulů
z Cinemety). Tenhle má vlastní manifest na `/c/<nastavení>/koncerty/manifest.json`,
vlastní tvar id (`nktc:<id koncertu>`) a nese katalog, meta i streamy. Seznam
sestavuje server (dashboard, `/concerts/*`) — doplněk se jen ptá na položky pro zdroje,
které má uživatel v nastavení zapnuté, a soubory posílá na společné `/play/`.
"""

import logging

from . import config, mapping

_LOGGER = logging.getLogger("nokturno")

ID = "cz.nokturno.koncerty"
PREFIX = "nktc:"
KATALOG = "nokturno.koncerty"
NOVE = "nokturno.koncerty.nove"   # 50 naposledy schválených (dashboard `/concerts/recent`), bez stránkování
KATALOGY = (NOVE, KATALOG)
TYP = "movie"
STRANKA = 100   # tolik vrací dashboard na jeden `skip`
LOGO = "https://raw.githubusercontent.com/matata86/plugin.video.nokturno/main/resources/media/icon2.png"
# zdroj → klíč nastavení, který ho zapíná (jen zdroje, ze kterých server koncerty sbírá)
ZDROJE = (("webshare", "ws_username"), ("hellspy", "hs_enabled"), ("fastshare", "fs_username"))


def zdroje(options):
    """Zapnuté zdroje koncertů podle nastavení v adrese, bez jádra."""
    o = options or {}
    return [zdroj for zdroj, klic in ZDROJE if (o.get(klic) if klic == "hs_enabled" else str(o.get(klic) or "").strip())]


def cislo(item_id):
    """`nktc:123` → 123, jinak None."""
    if not isinstance(item_id, str) or not item_id.startswith(PREFIX):
        return None
    try:
        n = int(item_id[len(PREFIX):])
    except ValueError:
        return None
    return n if n > 0 else None


def _nazev(polozka):
    rok = f" ({polozka['year']})" if polozka.get("year") else ""
    return f"{polozka['artist']} – {polozka['title']}{rok}"


class Koncerty:
    def __init__(self, dash):
        self.dash = dash   # nokturno_core.lib.dash_api.DashApi

    def manifest(self, verze, options):
        srcs = zdroje(options)
        popis = "Hudební koncerty z WebShare, HellSpy a FastShare — záznamy vystoupení podle interpreta, bez IMDb."
        if srcs:
            popis += " Nastavené zdroje: " + ", ".join(config.NAZVY_ZDROJU[s] for s in srcs) + "."
        else:
            popis += " ⚠️ V nastavení není zapnutý žádný zdroj, který koncerty umí (WebShare, HellSpy, FastShare)."
        return {
            "id": ID,
            "version": mapping.manifest_version(verze),
            "name": "Nokturno Koncerty",
            "description": popis,
            "logo": LOGO,
            "resources": ["catalog", "meta", "stream"],
            "types": [TYP],
            "idPrefixes": [PREFIX],
            "catalogs": [{"type": TYP, "id": NOVE, "name": "Koncerty – nově přidané"},
                         {"type": TYP, "id": KATALOG, "name": "Koncerty",
                          "extra": [{"name": "search", "isRequired": False}, {"name": "skip", "isRequired": False}]}],
            "behaviorHints": {"configurable": False, "configurationRequired": not srcs},
        }

    def katalog(self, options, search="", skip=0, katalog=KATALOG):
        srcs = zdroje(options)
        if not srcs or (katalog == NOVE and (search or skip)):
            return {"metas": []}
        try:
            if katalog == NOVE:
                polozky = self.dash.concert_recent(srcs)
            else:
                polozky, _celkem = self.dash.concert_items(srcs, search=search, skip=skip)
        except Exception as err:  # noqa: BLE001 – výpadek dashboardu = prázdný katalog, ne chyba služby
            _LOGGER.warning("katalog koncertů: %s", err)
            return {"metas": []}
        return {"metas": [self._nahled(p) for p in polozky]}

    @staticmethod
    def _nahled(p):
        return {"id": f"{PREFIX}{p['id']}", "type": TYP, "name": _nazev(p), "posterShape": "landscape",
                "releaseInfo": str(p["year"]) if p.get("year") else "",
                "description": "Zdroje: " + ", ".join(config.NAZVY_ZDROJU[s] for s in p.get("sources") or [])}

    def _koncert(self, options, item_id):
        n = cislo(item_id)
        srcs = zdroje(options)
        if n is None or not srcs:
            return None
        try:
            return self.dash.concert(n, srcs)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("koncert %s: %s", item_id, err)
            return None

    def meta(self, options, item_id):
        k = self._koncert(options, item_id)
        if not k:
            return None
        radky = [f"{f['name']} — {mapping_velikost(f['size'])} · {config.NAZVY_ZDROJU[f['source']]}" for f in k["files"]]
        m = self._nahled(k | {"sources": sorted({f["source"] for f in k["files"]})})
        m["description"] = "\n".join(radky)
        return {"meta": m}

    def streamy(self, options, item_id, odkaz, primy=None):
        """Každý soubor koncertu jako stream — `odkaz`/`primy` jsou stavitele z routeru,
        tytéž jako u hlavního doplňku (`/play/`, hlavičky FastShare)."""
        k = self._koncert(options, item_id)
        if not k:
            return {"streams": []}
        popisy = [{"url": f["ref"], "file": f["name"], "source": config.NAZVY_ZDROJU[f["source"]],
                   "size_gb": round(f["size"] / 1000 ** 3, 2) if f.get("size") else 0,
                   "length_min": (f.get("duration") or 0) // 60} for f in k["files"]]
        out = mapping.streams_response(popisy, odkaz, primy=primy)
        for s in out["streams"]:
            s["name"] = "Koncerty"
        return out


def mapping_velikost(size):
    return f"{size / 1000 ** 3:.1f} GB" if size else "?"
