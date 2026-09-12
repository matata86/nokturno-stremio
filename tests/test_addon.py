"""Kontrola vrstvy nad jádrem — bez sítě a bez účtů.

    python3 -m unittest discover -s tests -v

Jádro má vlastní testy v repu `nokturno-core`. Tady se ověřuje jen to, co je
vlastní doplňku: převod streamů do podoby pro Stremio, rozcestník a odmítání
odkazů, které by se neměly přehrát.
"""
import logging
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nokturno import config, mapping                      # noqa: E402
from nokturno.routes import Router                        # noqa: E402
from nokturno.core.engine import NokturnoError            # noqa: E402

def setUpModule():
    # test výpadku zdroje záměrně vyvolá chybu, kterou router loguje — ve výstupu testů
    # by to vypadalo jako skutečný problém
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


ZAKLAD = "http://addon.lan:7127"
# jak vypadá stream z Engine._describe()
POPIS = {
    "url": "ws:abc123", "file": "Matrix.1999.1080p.CZ.mkv", "source": "WebShare",
    "quality": "Full HD", "size_gb": 4.21, "bitrate": 8.5, "bitrate_est": True,
    "langs": ["CZ", "EN"], "channels": {"CZ": 5.1}, "subs": ["CZ"], "subtitles": ["ws:sub1"],
}


class FalesnyEngine:
    """Jádro nahrazené tak, aby testy nešly na síť."""

    VSE_VYPNUTO = {"luna": False, "sosac": False, "webshare": False, "hellspy": False, "torrent": False}

    def __init__(self, streamy=None, chyba=None, odkaz="https://cdn.example/film.mkv", zdroje=None):
        self.streamy = streamy if streamy is not None else [POPIS]
        self.chyba = chyba
        self.odkaz = odkaz
        self.dotazy = []
        self.options = {}
        self._zdroje = {**self.VSE_VYPNUTO, "webshare": True} if zdroje is None else {**self.VSE_VYPNUTO, **zdroje}

    def sources(self):
        return self._zdroje

    def streams(self, ctype, item_id, loose_fallback=False):
        self.dotazy.append((ctype, item_id))
        self.loose_fallback = loose_fallback
        if self.chyba:
            raise self.chyba
        return self.streamy

    def resolve(self, url):
        if self.chyba:
            raise self.chyba
        return self.odkaz


class FalesneEnginy:
    """Správa jader nahrazená jedním falešným, ale pamatuje si, s čím se volalo."""

    def __init__(self, engine):
        self.engine = engine
        self.pozadovana_nastaveni = []
        self.vychozi_options = {"ws_username": "z-prostredi"}

    def pro(self, options=None):
        self.pozadovana_nastaveni.append(options)
        return self.engine

    def __len__(self):
        return 1


def router(**kw):
    enginy = FalesneEnginy(FalesnyEngine(**kw))
    r = Router(enginy)
    r.enginy_test = enginy
    r.engine = enginy.engine
    return r


# adresa s nastavením, jakou vyrobí formulář
NASTAVENI = config.from_mapping({"ws_username": "uzivatel", "ws_password": "tajne"})
KOUSEK = config.encode(NASTAVENI)


class TestManifest(unittest.TestCase):
    def test_hlasi_streamy_pro_filmy_i_serialy(self):
        m = router().route(f"/c/{KOUSEK}/manifest.json", ZAKLAD).data
        self.assertEqual(m["resources"], ["stream"])
        self.assertEqual(m["types"], ["movie", "series"])
        self.assertTrue(m["behaviorHints"]["configurable"], "Stremio má nabídnout formulář")

    def test_neomezuje_se_na_imdb_id(self):
        """S idPrefixes ["tt"] se Stremio neptalo na tituly z cizích katalogů."""
        m = router().route(f"/c/{KOUSEK}/manifest.json", ZAKLAD).data
        self.assertNotIn("idPrefixes", m)

    def test_bez_zdroju_si_rekne_o_nastaveni(self):
        prazdny = router(zdroje={})
        self.assertTrue(prazdny.route("/manifest.json", ZAKLAD).data["behaviorHints"]["configurationRequired"])
        self.assertFalse(router().route("/manifest.json", ZAKLAD).data["behaviorHints"]["configurationRequired"])


class TestNastaveniVAdrese(unittest.TestCase):
    """Fáze 4: účty nese adresa, takže každý hledá pod svým."""

    def test_nastaveni_z_adresy_dojde_k_jadru(self):
        r = router()
        r.route(f"/c/{KOUSEK}/manifest.json", ZAKLAD)
        self.assertEqual(r.enginy_test.pozadovana_nastaveni[-1], NASTAVENI)

    def test_bez_prefixu_se_bere_vychozi(self):
        """Adresy nasazené před fází 4 musí fungovat dál."""
        r = router()
        r.route("/manifest.json", ZAKLAD)
        self.assertIsNone(r.enginy_test.pozadovana_nastaveni[-1])

    def test_nectitelne_nastaveni_je_404(self):
        self.assertEqual(router().route("/c/rozbite!!/manifest.json", ZAKLAD).status, 404)

    def test_odkaz_na_prehrani_nese_stejne_nastaveni(self):
        """Jinak by se soubor rozklíčoval cizím účtem, nebo vůbec."""
        odpoved = router().route(f"/c/{KOUSEK}/stream/movie/tt1.json", ZAKLAD)
        url = odpoved.data["streams"][0]["url"]
        self.assertTrue(url.startswith(f"{ZAKLAD}/c/{KOUSEK}/play/"), url)

    def test_formular_se_predvyplni_z_adresy(self):
        html = router().route(f"/c/{KOUSEK}/configure", ZAKLAD).html
        self.assertIn("uzivatel", html, "formulář má ukázat, co v adrese je")
        self.assertIn(ZAKLAD, html)

    def test_formular_jde_i_bez_nastaveni(self):
        self.assertEqual(router().route("/configure", ZAKLAD).status, 200)

    def test_formular_neukazuje_ucty_instance(self):
        """Na sdílené instanci by je jinak viděl každý, kdo formulář otevře."""
        self.assertNotIn("z-prostredi", router().route("/configure", ZAKLAD).html)

    def test_predvyplneni_jde_zapnout(self):
        r = router()
        r.predvyplnit = True
        self.assertIn("z-prostredi", r.route("/configure", ZAKLAD).html)

    def test_stejne_nastaveni_da_stejnou_adresu(self):
        """Jinak by se doplněk po přenastavení uživateli zdvojil."""
        jinak_serazene = {"ws_password": "tajne", "ws_username": "uzivatel"}
        self.assertEqual(config.encode(config.from_mapping(jinak_serazene)), KOUSEK)


class TestStreamy(unittest.TestCase):
    def test_film(self):
        r = router()
        odpoved = r.route("/stream/movie/tt0133093.json", ZAKLAD)
        self.assertEqual(odpoved.status, 200)
        self.assertEqual(r.engine.dotazy, [("movie", "tt0133093")])
        self.assertEqual(len(odpoved.data["streams"]), 1)

    def test_epizoda_dojde_k_jadru_cela(self):
        """Jádro si `tt…:S:E` rozpadne samo — doplněk do id nesmí sahat."""
        r = router()
        r.route("/stream/series/tt0903747:1:1.json", ZAKLAD)
        self.assertEqual(r.engine.dotazy, [("series", "tt0903747:1:1")])

    def test_serial_bez_epizody_je_chyba(self):
        self.assertEqual(router().route("/stream/series/tt0903747.json", ZAKLAD).status, 400)

    def test_cizi_id_vrati_prazdno(self):
        """Id z cizího katalogu neumíme přeložit na název, ale nesmíme spadnout."""
        r = router()
        self.assertEqual(r.route("/stream/movie/kitsu:42.json", ZAKLAD).data, {"streams": []})
        self.assertEqual(r.engine.dotazy, [], "k jádru se takový dotaz nemá dostat")

    def test_id_sosace_projde_k_jadru(self):
        """Sosáčova id jádro umí, takže je nezahazujeme jako cizí."""
        r = router()
        r.route("/stream/movie/sosacd_m_6fcb548442d588f6dd73.json", ZAKLAD)
        self.assertEqual(len(r.engine.dotazy), 1)

    def test_vypadek_zdroje_neni_chyba_sluzby(self):
        """Stremio má ukázat prázdno a jít dál, ne chybu."""
        for chyba in (NokturnoError("není nastaveno"), RuntimeError("spadlo to")):
            odpoved = router(chyba=chyba).route("/stream/movie/tt1.json", ZAKLAD)
            self.assertEqual(odpoved.status, 200)
            self.assertEqual(odpoved.data, {"streams": []})

    def test_neznamy_typ(self):
        self.assertEqual(router().route("/stream/kniha/tt1.json", ZAKLAD).status, 404)


class TestPrehrani(unittest.TestCase):
    def test_presmeruje_na_skutecny_soubor(self):
        odpoved = router().route("/play/" + mapping.zakoduj("ws:abc"), ZAKLAD)
        self.assertEqual(odpoved.status, 302)
        self.assertEqual(odpoved.location, "https://cdn.example/film.mkv")

    def test_odmitne_cizi_schema(self):
        """Bez kontroly by `resolve()` neznámou hodnotu vrátil a šlo by přesměrovat kamkoli."""
        for nebezpecne in ("file:///etc/passwd", "gopher://x", "/etc/passwd"):
            odpoved = router().route("/play/" + mapping.zakoduj(nebezpecne), ZAKLAD)
            self.assertEqual(odpoved.status, 400, nebezpecne)

    def test_odmitne_neplatny_payload(self):
        self.assertEqual(router().route("/play/nesmysl!!", ZAKLAD).status, 400)

    def test_nedostupny_soubor_je_502(self):
        odpoved = router(chyba=NokturnoError("WebShare soubor nevydá")).route(
            "/play/" + mapping.zakoduj("ws:abc"), ZAKLAD)
        self.assertEqual(odpoved.status, 502)


class TestPrevod(unittest.TestCase):
    def setUp(self):
        self.objekt = mapping.stream_object(POPIS, lambda u: f"{ZAKLAD}/play/{mapping.zakoduj(u)}")

    def test_odkaz_vede_na_sluzbu_ne_na_zdroj(self):
        """Odkazy WebShare platí jen chvíli, takže se nesmí vydávat dopředu."""
        self.assertTrue(self.objekt["url"].startswith(f"{ZAKLAD}/play/"))
        self.assertNotIn("ws:", self.objekt["url"])

    def test_vlevo_kvalita_vpravo_podrobnosti(self):
        self.assertEqual(self.objekt["name"], "Nokturno\nFull HD")
        popis = self.objekt["description"]
        self.assertIn("Matrix.1999.1080p.CZ.mkv", popis)
        self.assertIn("4.2 GB", popis)
        self.assertIn("~8.5 Mb/s", popis, "odhadnutý bitrate má být přiznaný")
        self.assertIn("WebShare", popis)

    def test_jazyky_jako_vlajecky(self):
        popis = self.objekt["description"]
        self.assertIn("🇨🇿 5.1", popis, "zvuk s počtem kanálů")
        self.assertIn("🇬🇧", popis)
        self.assertIn("💬 🇨🇿", popis, "titulky")

    def test_neznamy_jazyk_zustane_kodem(self):
        """Chybějící vlaječka nesmí jazyk spolknout."""
        objekt = mapping.stream_object({**POPIS, "langs": ["XX"], "channels": {}}, lambda u: u)
        self.assertIn("XX", objekt["description"])

    def test_hdr_a_atmos_z_nazvu_souboru(self):
        """Jádro je nezná — žádný zdroj je nehlásí, leží jen v názvu."""
        objekt = mapping.stream_object(
            {**POPIS, "file": "Titanic.2160p.REMUX.DV.HDR.TrueHD.Atmos.mkv", "quality": "4K"},
            lambda u: u)
        self.assertEqual(objekt["name"], "Nokturno\n4K DV", "obraz patří vlevo ke kvalitě")
        self.assertIn("Atmos", objekt["description"])
        self.assertIn("TrueHD", objekt["description"])

    def test_delka_streamu(self):
        objekt = mapping.stream_object({**POPIS, "length_min": 194}, lambda u: u)
        self.assertIn("3:14", objekt["description"])
        objekt = mapping.stream_object({**POPIS, "length_min": 42, "length_est": True}, lambda u: u)
        self.assertIn("~42 min", objekt["description"])

    def test_napovedy_pro_prehravac(self):
        hints = self.objekt["behaviorHints"]
        self.assertEqual(hints["videoSize"], 4210000000)
        self.assertEqual(hints["filename"], "Matrix.1999.1080p.CZ.mkv")
        self.assertTrue(hints["notWebReady"], "mkv webový přehrávač nepřehraje")
        self.assertEqual(hints["bingeGroup"], "nokturno-webshare-full-hd")

    def test_titulky_taky_pres_sluzbu(self):
        self.assertEqual(len(self.objekt["subtitles"]), 1)
        self.assertEqual(self.objekt["subtitles"][0]["lang"], "ces")
        self.assertTrue(self.objekt["subtitles"][0]["url"].startswith(f"{ZAKLAD}/play/"))

    def test_stream_bez_odkazu_se_zahodi(self):
        self.assertIsNone(mapping.stream_object({**POPIS, "url": ""}, lambda u: u))

    def test_neoverena_shoda_je_videt(self):
        """Volnější fulltext může vrátit jiný titul, který název jen obsahuje."""
        objekt = mapping.stream_object({**POPIS, "loose": True}, lambda u: u)
        self.assertTrue(objekt["name"].startswith("Nokturno ⚠️"))
        self.assertIn("neověřená shoda", objekt["description"])

    def test_overena_shoda_se_neznaci(self):
        objekt = mapping.stream_object(POPIS, lambda u: u)
        self.assertEqual(objekt["name"], "Nokturno\nFull HD")
        self.assertNotIn("neověřená", objekt["description"])

    def test_volny_fulltext_se_zapina(self):
        """Bez něj by u titulu, který přísný filtr nezná, zůstal prázdný seznam."""
        r = router()
        r.route("/stream/movie/tt1.json", ZAKLAD)
        self.assertTrue(r.engine.loose_fallback)

    def test_mp4_je_pro_web_v_poradku(self):
        objekt = mapping.stream_object({**POPIS, "file": "film.mp4"}, lambda u: u)
        self.assertNotIn("notWebReady", objekt["behaviorHints"])


class TestNastaveni(unittest.TestCase):
    def test_prazdne_prostredi_da_rozumne_vychozi(self):
        options = config.from_environ({})
        self.assertEqual(options["sort_streams"], "quality")
        self.assertTrue(options["hs_enabled"], "HellSpy nepotřebuje účet, ať je zapnutý")

    def test_prepinace_z_textu(self):
        options = config.from_environ({"NOKTURNO_HIDE_SD": "ano", "NOKTURNO_PREF_SURROUND": "0"})
        self.assertTrue(options["hide_sd"])
        self.assertFalse(options["pref_surround"])

    def test_nesmyslna_hodnota_spadne_na_vychozi(self):
        options = config.from_environ({"NOKTURNO_SORT": "podle-barvy", "NOKTURNO_PREF_LANG": "XX"})
        self.assertEqual(options["sort_streams"], "quality")
        self.assertEqual(options["pref_lang"], "")

    def test_klice_sedi_na_to_co_cte_engine(self):
        """Překlep v klíči by se neprojevil chybou, jen tichým ignorováním nastavení."""
        zdroj = (ROOT / "nokturno" / "core" / "engine.py").read_text(encoding="utf-8")
        chybi = [k for k in config.PROSTREDI.values() if f'"{k}"' not in zdroj and k != "hs_enabled"]
        self.assertEqual(chybi, [], f"engine tyhle klíče nezná: {chybi}")


if __name__ == "__main__":
    unittest.main()
