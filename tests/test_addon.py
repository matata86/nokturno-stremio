"""Kontrola vrstvy nad jádrem — bez sítě a bez účtů.

    python3 -m unittest discover -s tests -v

Jádro má vlastní testy v repu `nokturno-core`. Tady se ověřuje jen to, co je
vlastní doplňku: převod streamů do podoby pro Stremio, rozcestník a odmítání
odkazů, které by se neměly přehrát.
"""
import logging
import os
import pathlib
import sys
import tempfile
import time
import unittest
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nokturno import config, mapping, routes              # noqa: E402
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

    def streams(self, ctype, item_id):
        self.dotazy.append((ctype, item_id))
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

    def pro(self, options=None, verejny=False, klient=""):
        self.pozadovana_nastaveni.append(options)
        self.verejny = verejny
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
# nastavení jen s HellSpy — sdílí ho spousta lidí, limity se proto počítají na IP
KOUSEK_HS = config.encode(config.from_mapping({"hs_enabled": True}))


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

    def test_verze_v_manifestu_je_ciste_semver(self):
        """Betasufix (5.2.1b1) v poli 'version' rozbíjí parser oficiálního Stremio
        klienta — musí zůstat jen 'major.minor.patch'."""
        self.assertEqual(mapping.manifest_version("5.2.1b1"), "5.2.1")
        self.assertEqual(mapping.manifest_version("5.2.1~beta1"), "5.2.1")
        self.assertEqual(mapping.manifest_version("5.2.1"), "5.2.1")

    def test_bez_zdroju_si_rekne_o_nastaveni(self):
        prazdny = router()
        prazdny.enginy_test.vychozi_options = {}
        self.assertTrue(prazdny.route("/manifest.json", ZAKLAD).data["behaviorHints"]["configurationRequired"])
        self.assertFalse(router().route("/manifest.json", ZAKLAD).data["behaviorHints"]["configurationRequired"])

    def test_manifest_nezaklada_jadro(self):
        """Jeden GET na náhodnou adresu dřív založil jádro i složku na disku navždy."""
        r = router()
        data = r.route(f"/c/{KOUSEK}/manifest.json", ZAKLAD).data
        self.assertEqual(r.enginy_test.pozadovana_nastaveni, [])
        self.assertIn("WebShare", data["description"], "zdroje se poznají z nastavení bez jádra")
        from nokturno.enginy import Enginy
        tmp = tempfile.mkdtemp()
        Router(Enginy(tmp, {})).route(f"/c/{KOUSEK}/manifest.json", ZAKLAD)
        self.assertEqual(sorted(pathlib.Path(tmp).iterdir()), [], "žádná složka jádra")


class TestNastaveniVAdrese(unittest.TestCase):
    """Fáze 4: účty nese adresa, takže každý hledá pod svým."""

    def test_nastaveni_z_adresy_dojde_k_jadru(self):
        r = router()
        r.route(f"/c/{KOUSEK}/stream/movie/tt1.json", ZAKLAD)
        self.assertEqual(r.enginy_test.pozadovana_nastaveni[-1], NASTAVENI)

    def test_bez_prefixu_se_bere_vychozi(self):
        """Adresy nasazené před fází 4 musí fungovat dál."""
        r = router()
        r.route("/stream/movie/tt1.json", ZAKLAD)
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
        """Bez kontroly by `resolve()` neznámou hodnotu vrátil a šlo by přesměrovat kamkoli —
        včetně hotových http(s) odkazů, které jádro vrací beze změny (2026-09-14)."""
        for nebezpecne in ("file:///etc/passwd", "gopher://x", "/etc/passwd",
                           "https://evil.example/x", "http://evil.example/", "HTTP://evil.example/"):
            odpoved = router(odkaz=nebezpecne).route("/play/" + mapping.zakoduj(nebezpecne), ZAKLAD)
            self.assertEqual(odpoved.status, 400, nebezpecne)

    def test_hotovy_odkaz_se_vydava_rovnou_ne_pres_play(self):
        """Titulky Sledujteto přicházejí jako hotové https odkazy — do `/play/` nepatří."""
        popis = {**POPIS, "url": "https://cdn.sledujteto.cz/film.mp4", "subtitles": ["ws:s1", "https://cdn/t.srt", "x:y"]}
        objekt = mapping.stream_object(popis, lambda u: f"{ZAKLAD}/play/{mapping.zakoduj(u)}")
        self.assertEqual(objekt["url"], "https://cdn.sledujteto.cz/film.mp4")
        self.assertEqual([t["url"] for t in objekt["subtitles"]],
                         [f"{ZAKLAD}/play/{mapping.zakoduj('ws:s1')}", "https://cdn/t.srt"])

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

    def test_overena_shoda_se_neznaci(self):
        objekt = mapping.stream_object(POPIS, lambda u: u)
        self.assertEqual(objekt["name"], "Nokturno\nFull HD")
        self.assertNotIn("neověřená", objekt["description"])

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
        # `katalogy` nečte jádro, ale doplněk sám (nokturno/katalogy.py)
        chybi = [k for k in config.PROSTREDI.values() if f'"{k}"' not in zdroj and k not in ("hs_enabled", "katalogy")]
        self.assertEqual(chybi, [], f"engine tyhle klíče nezná: {chybi}")


class TestVerejnyPristup(unittest.TestCase):
    """Přes Tailscale Funnel je doplněk na internetu — výchozí účty instance nesmí ven."""

    def test_bez_nastaveni_zvenku_nic_nenajde(self):
        r = router()
        odpoved = r.route("/stream/movie/tt1.json", ZAKLAD, verejny=True)
        self.assertEqual(odpoved.status, 403)
        self.assertEqual(r.engine.dotazy, [], "jádro s účty instance se nesmí ani zeptat")
        self.assertEqual(r.route("/play/eHh4", ZAKLAD, verejny=True).status, 403)

    def test_manifest_zvenku_chce_nastaveni(self):
        r = router()
        data = r.route("/manifest.json", ZAKLAD, verejny=True).data
        self.assertTrue(data["behaviorHints"]["configurationRequired"])
        self.assertNotIn("Nastavené zdroje", data["description"], "neprozradí, co má instance nastavené")
        self.assertEqual(r.enginy_test.pozadovana_nastaveni, [])

    def test_formular_zvenku_se_nepredvyplni(self):
        r = router()
        r.predvyplnit = True
        self.assertNotIn("z-prostredi", r.route("/configure", ZAKLAD, verejny=True).html)
        self.assertIn("z-prostredi", r.route("/configure", ZAKLAD).html, "z domácí sítě dál ano")

    def test_uvod_je_rozcestnik_a_neprozradi_zdroje(self):
        r = router()
        html = r.route("/", ZAKLAD, verejny=True).html
        self.assertIn(f"{ZAKLAD}/configure", html)
        for repo in ("plugin.video.nokturno", "nokturno-ha", "nokturno-stremio", "nokturno-core"):
            self.assertIn(f"github.com/matata86/{repo}", html)
        self.assertNotIn("__ZAKLAD__", html)
        self.assertNotIn("__VERZE__", html)
        self.assertEqual(r.enginy_test.pozadovana_nastaveni, [], "úvod jádro nezakládá")

    def test_formular_umi_nuvio(self):
        html = router().route("/configure", ZAKLAD).html
        self.assertIn('"nuvio://"', html)
        self.assertIn("Streamlet", html)

    def test_s_vlastnim_nastavenim_zvenku_funguje(self):
        r = router()
        self.assertEqual(r.route(f"/c/{KOUSEK}/stream/movie/tt1.json", ZAKLAD, verejny=True).status, 200)
        self.assertEqual(r.enginy_test.pozadovana_nastaveni[-1], NASTAVENI)

    def test_rozpoznani_verejneho_pozadavku(self):
        from nokturno.server import je_verejny
        self.assertTrue(je_verejny({"Tailscale-Funnel-Request": "?1"}, "127.0.0.1"))
        self.assertTrue(je_verejny({"Tailscale-Funnel-Request": "?1", "Tailscale-User-Login": "x@y"}, "127.0.0.1"),
                        "značka Funnelu vyhrává")
        self.assertFalse(je_verejny({"Tailscale-User-Login": "x@y"}, "127.0.0.1"), "tailnet přes serve")
        self.assertTrue(je_verejny({}, "127.0.0.1"), "přes proxy bez identity = pochybnost = veřejný")
        self.assertFalse(je_verejny({}, "192.168.1.50"), "přímo z LAN jako dřív")


class FalesnyWebshare:
    def __init__(self, user, password):
        self.user, self.password = user, password

    def login(self):
        if self.password != "spravne":
            raise NokturnoError("Wrong password")
        return "token"

    def account_status(self):
        return {"vip": True, "days": 42, "until": "2026-10-25 12:00:00"}


class TestOvereniUctu(unittest.TestCase):
    def _check(self, nastaveni, verejny=False):
        r = router()
        r.ws_api = FalesnyWebshare
        kousek = config.encode(config.from_mapping(nastaveni))
        return r, r.route(f"/c/{kousek}/check", ZAKLAD, verejny=verejny)

    def test_spravny_ucet_s_vip(self):
        _r, odpoved = self._check({"ws_username": "u", "ws_password": "spravne"})
        self.assertEqual(odpoved.data["webshare"], {"ok": True, "vip": True, "days": 42, "until": "2026-10-25 12:00:00"})

    def test_spatne_heslo(self):
        _r, odpoved = self._check({"ws_username": "u", "ws_password": "spatne"})
        self.assertFalse(odpoved.data["webshare"]["ok"])
        self.assertIn("Wrong password", odpoved.data["webshare"]["chyba"])

    def test_streamuj_jen_hlasi_vyplneni(self):
        _r, odpoved = self._check({"streamuj_username": "u"})
        self.assertIsNone(odpoved.data["webshare"])
        self.assertEqual(odpoved.data["streamuj"], {"heslo": False})
        self.assertTrue(odpoved.data["hellspy"], "HellSpy je ve výchozím stavu zapnutý")

    def test_zvenku_s_vlastnim_nastavenim_jde(self):
        r, odpoved = self._check({"ws_username": "u", "ws_password": "spravne"}, verejny=True)
        self.assertTrue(odpoved.data["webshare"]["ok"])
        self.assertEqual(r.enginy_test.pozadovana_nastaveni, [], "kvůli ověření se jádro nezakládá")

    def test_zvenku_bez_nastaveni_neoveri_ucty_instance(self):
        r = router()
        r.ws_api = FalesnyWebshare
        self.assertEqual(r.route("/check", ZAKLAD, verejny=True).status, 403)


class TestBezLuny(unittest.TestCase):
    """Luna má vlastní doplněk do Stremia a její odkazy vedou do domácí sítě."""

    def test_z_adresy_se_neprevezme(self):
        options = config.from_mapping({"luna_url": "http://192.168.1.10:7126", "luna_token": "e1.x", "ws_username": "u"})
        self.assertNotIn("luna_url", options)
        self.assertNotIn("luna_token", options)
        self.assertEqual(options["ws_username"], "u")

    def test_z_prostredi_se_neprevezme(self):
        options = config.from_environ({"NOKTURNO_LUNA_URL": "http://x:7126", "NOKTURNO_LUNA_TOKEN": "e1.x"})
        self.assertNotIn("luna_url", options)

    def test_logo_manifestu_existuje_v_repu_doplnku(self):
        logo = mapping.manifest("0").get("logo", "")
        self.assertTrue(logo.endswith("/resources/media/icon2.png"), logo)


class TestKatalogy(unittest.TestCase):
    """Volitelné katalogy: jen zvolené v manifestu, jedna sdílená cache, bez jádra."""

    def setUp(self):
        import tempfile
        from nokturno.katalogy import Katalogy
        self.k = Katalogy(tempfile.mkdtemp())
        volani = self.volani = []

        class Sosac:
            def catalog(self, ctype, cid, skip=0, page=100):
                volani.append((ctype, cid, skip))
                return [{"id": "sosacd_m_x", "imdb_id": "tt0133093", "name": "Matrix", "year": "1999",
                         "poster": "https://img/x.jpg", "description": "popis", "genres": ["Sci-Fi"], "imdbRating": 8.66},
                        {"id": "sosacd_m_y", "name": "Bez IMDb", "year": "2026"}]
        self.k.sosac = Sosac()
        self.r = router()
        self.r.katalogy = self.k

    def test_bez_klice_tmdb_jen_sosac_a_trend(self):
        self.assertTrue(self.k.dostupne())
        self.assertEqual({r[2] for r in self.k.dostupne()}, {"sosac", "trend"})

    def test_manifest_jen_zvolene_katalogy(self):
        m = self.r.route(f"/c/{KOUSEK}/manifest.json", ZAKLAD).data
        self.assertEqual((m["resources"], m["catalogs"]), (["stream"], []), "bez volby se manifest nemění")
        kousek = config.encode(config.from_mapping({"ws_username": "u", "katalogy": "sosac.nove.dabing,neexistuje,tmdb.trendy.filmy"}))
        m = self.r.route(f"/c/{kousek}/manifest.json", ZAKLAD).data
        self.assertEqual(m["resources"], ["stream", "catalog"])
        self.assertEqual([(c["type"], c["id"]) for c in m["catalogs"]], [("movie", "nokturno.sosac.nove.dabing")])

    def test_katalog_sdileny_cachovany_a_strankovany(self):
        cesta = "/catalog/movie/nokturno.sosac.nove.dabing.json"
        data = self.r.route(f"/c/{KOUSEK}{cesta}", ZAKLAD).data
        self.assertEqual(data["metas"], [{"id": "tt0133093", "type": "movie", "name": "Matrix", "posterShape": "poster",
                                          "poster": "https://images.metahub.space/poster/medium/tt0133093/img",
                                          "description": "popis", "releaseInfo": "1999",
                                          "genres": ["Sci-Fi"], "imdbRating": "8.7"}])
        from nokturno.katalogy import nahled
        tmdb = nahled("movie", {"id": "tt1", "name": "X", "poster": "https://image.tmdb.org/t/p/w500/a.jpg",
                                "background": "https://image.tmdb.org/t/p/w1280/b.jpg"})
        self.assertEqual((tmdb["poster"], tmdb["background"]),
                         ("https://image.tmdb.org/t/p/w500/a.jpg", "https://image.tmdb.org/t/p/w1280/b.jpg"),
                         "plakát z TMDB zůstává, Sosáčův (přesměruje na web) se nahradí metahubem")
        jina = config.encode(config.from_mapping({"ws_username": "nekdo-jiny"}))
        self.r.route(f"/c/{jina}{cesta}", ZAKLAD)
        self.assertEqual(len(self.volani), 1, "jiná adresa bere tutéž cache")
        self.r.route(f"/c/{KOUSEK}/catalog/movie/nokturno.sosac.nove.dabing/skip=100.json", ZAKLAD)
        self.assertEqual(self.volani[-1], ("movie", "moviesrecentlyadded_dub", 100))
        self.assertEqual(self.r.enginy_test.pozadovana_nastaveni, [], "katalog nezakládá jádro")

    def test_nejsledovanejsi_tento_tyden_z_dashboardu(self):
        """`trend.nejsledovanejsi.*` — stejný žebříček jako v Kodi menu, sdílená
        cache jako ostatní katalogy (2026-09-15)."""
        volani_trend = []

        class Trend:
            def catalog(self, ctype, cid, skip=0):
                volani_trend.append((ctype, cid, skip))
                return [{"id": "tt0133093", "type": ctype, "name": "Matrix", "year": "1999",
                         "poster": "https://img/x.jpg", "description": "popis",
                         "genres": ["Sci-Fi"], "imdbRating": 8.66}]
        self.k.trend = Trend()
        kousek = config.encode(config.from_mapping({"ws_username": "u", "katalogy": "trend.nejsledovanejsi.filmy"}))
        m = self.r.route(f"/c/{kousek}/manifest.json", ZAKLAD).data
        self.assertEqual([(c["type"], c["id"]) for c in m["catalogs"]],
                         [("movie", "nokturno.trend.nejsledovanejsi.filmy")])
        data = self.r.route(f"/c/{kousek}/catalog/movie/nokturno.trend.nejsledovanejsi.filmy.json", ZAKLAD).data
        self.assertEqual(data["metas"][0]["id"], "tt0133093")
        from nokturno.core.lib.trend_api import CATALOG_ID
        self.assertEqual(volani_trend, [("movie", CATALOG_ID, 0)])

    def test_serialy_podle_jazyka_z_pozadi(self):
        """Sosáč u seriálů jazyk neuvádí — server ověří streamy nejnovějšího dílu (5.2.15)."""
        from nokturno.katalogy import Katalogy
        dotazy = []
        # `_jazykove()` spustí přepočet a hned čte cache; s falešným jádrem doběhne
        # vlákno tak rychle, že první dotaz občas vrátil rovnou hotový výsledek a test
        # padal jednou za pár běhů. Brána drží přepočet, dokud si první dotaz neověříme.
        pustit = __import__("threading").Event()

        class Engine:
            def raw_streams(self, ctype, item_id, probe_audio=True, **kw):
                pustit.wait(5)
                dotazy.append((ctype, item_id, probe_audio))
                return {"tt1:1:6": [{"langs": ["EN"], "subs": ["CZ"]}, {"langs": ["CZ"]}],
                        "tt2:2:1": [{"langs": ["EN"], "subs": ["SK"]}],
                        "tt3:1:1": [{"langs": ["EN"], "subs": ["EN"]}]}.get(item_id, [])

        class Sosac:
            def recent_series(self, limit=60):
                return [({"imdb_id": f"tt{i}", "name": f"S{i}", "year": "2026"}, s, e)
                        for i, s, e in ((1, 1, 6), (2, 2, 1), (3, 1, 1))]

        k = Katalogy(tempfile.mkdtemp(), engine=lambda: Engine())
        k.sosac = Sosac()
        self.assertEqual(k.polozky("series", "nokturno.sosac.nove.serialy.dabing"), [], "první dotaz jen spustí přepočet")
        pustit.set()
        for vlakno in [v for v in __import__("threading").enumerate() if v.name == "katalog-serialy-jazyk"]:
            vlakno.join(5)
        self.assertEqual([m["id"] for m in k.polozky("series", "nokturno.sosac.nove.serialy.dabing")], ["tt1"])
        self.assertEqual([m["id"] for m in k.polozky("series", "nokturno.sosac.nove.serialy.titulky")], ["tt2"])
        self.assertEqual(dotazy[0], ("series", "tt1:1:6", False))
        self.assertEqual(len(dotazy), 3, "hotový výsledek se znovu nepočítá")
        # bez jádra instance se seriálové katalogy nenabízejí
        self.assertNotIn("jazyk", {r[2] for r in self.k.dostupne()})
        self.assertIsNone(self.k.polozky("series", "nokturno.sosac.nove.serialy.dabing"))

    def test_nazvy_filmu_a_serialu_rozlisene(self):
        from nokturno.katalogy import Katalogy
        k = Katalogy(tempfile.mkdtemp(), engine=lambda: None)
        nazvy = {f["klic"]: f["nazev"] for f in k.formular()}
        self.assertEqual(nazvy["sosac.nove.dabing"], "Nově přidané filmy s CZ dabingem")
        self.assertEqual(nazvy["sosac.nove.serialy.titulky"], "Nově přidané seriály s CZ titulky")

    def test_neznamy_katalog_a_verejny_bez_nastaveni(self):
        self.assertEqual(self.r.route(f"/c/{KOUSEK}/catalog/series/nokturno.sosac.nove.dabing.json", ZAKLAD).status, 404)
        self.assertEqual(self.r.route(f"/c/{KOUSEK}/catalog/movie/nokturno.tmdb.trendy.filmy.json", ZAKLAD).status, 404)
        self.assertEqual(self.r.route("/catalog/movie/nokturno.sosac.nove.dabing.json", ZAKLAD, verejny=True).status, 403)

    def test_formular_nabizi_katalogy_instance(self):
        html = self.r.route("/configure", ZAKLAD).html
        self.assertNotIn("__KATALOGY__", html)
        self.assertIn('"sosac.nove.dabing"', html)
        self.assertNotIn("tmdb.trendy.filmy", html, "bez klíče TMDB se jeho katalogy nenabízejí")

    def test_formular_nabizi_jen_doporucene_ale_stare_dal_funguji(self):
        """2026-09-15: nabídka sjednocená s Kodi menu (`DOPORUCENE`) — starší
        katalogy z `SEZNAM` (např. „Nejpopulárnější filmy“) se novým uživatelům
        ve formuláři nenabízejí, ale kdo je má uložené v adrese z dřívějška, dál
        mu fungují (manifest i výpis), nic se mu nerozbije."""
        html = self.r.route("/configure", ZAKLAD).html
        self.assertNotIn("sosac.popularni.filmy", html)
        self.assertIn("sosac.popularni.filmy", {r[0] for r in self.k.dostupne()})
        kousek = config.encode(config.from_mapping({"ws_username": "u", "katalogy": "sosac.popularni.filmy"}))
        m = self.r.route(f"/c/{kousek}/manifest.json", ZAKLAD).data
        self.assertEqual([(c["type"], c["id"]) for c in m["catalogs"]], [("movie", "nokturno.sosac.popularni.filmy")])
        data = self.r.route(f"/c/{kousek}/catalog/movie/nokturno.sosac.popularni.filmy.json", ZAKLAD).data
        self.assertEqual(self.volani, [("movie", "moviesmostpopular", 0)])
        self.assertTrue(data["metas"])
        self.assertEqual(config.from_mapping({"katalogy": " b ,a,,a"})["katalogy"], "a,b")


class FalesnyFastshare:
    def __init__(self, login, password):
        self.password = password

    def login(self):
        if self.password != "spravne":
            raise NokturnoError("přihlášení se nepovedlo — zkontroluj jméno a heslo")
        return {"hash": "H", "unlimited": False, "credit_mb": 20480}


class TestFastshareVeStremiu(unittest.TestCase):
    def _check(self, nastaveni):
        r = router()
        r.fs_api = FalesnyFastshare
        kousek = config.encode(config.from_mapping(nastaveni))
        return r.route(f"/c/{kousek}/check", ZAKLAD).data["fastshare"]

    def test_overeni_hlasi_kredit(self):
        self.assertEqual(self._check({"fs_username": "u", "fs_password": "spravne"}),
                         {"ok": True, "neomezene": False, "kredit_mb": 20480})
        self.assertFalse(self._check({"fs_username": "u", "fs_password": "spatne"})["ok"])

    def test_stream_nese_cookie_v_proxyheaders(self):
        """Cookie z přihlášení pošle přehrávač sám — data netečou přes tenhle server."""
        adresa = "https://data4.fastshare.cloud/download.php?id=1"
        objekt = mapping.stream_object({"url": "fs:1:data4:10", "file": "film.mkv"}, lambda v: "/play/" + v,
                                       primy=lambda v: (adresa, {"Cookie": "FASTSHARE=H"}))
        self.assertEqual(objekt["url"], adresa)
        self.assertEqual(objekt["behaviorHints"]["proxyHeaders"], {"request": {"Cookie": "FASTSHARE=H"}})
        self.assertTrue(objekt["behaviorHints"]["notWebReady"])

    def test_play_uz_fastshare_neobsluhuje(self):
        odpoved = router().play(object(), mapping.zakoduj("fs:1:data4:10"))
        self.assertEqual(odpoved.status, 410, "proxy zrušená v 5.2.26 — starý odkaz má říct proč")

    def test_zdroj_z_nastaveni_a_prostredi(self):
        self.assertEqual(config.PROSTREDI["NOKTURNO_FS_USERNAME"], "fs_username")
        self.assertIn("FastShare", config.sources_from_options({"fs_username": "u"}))


class FalesneSledujteto:
    def __init__(self, email, password):
        self.password = password

    def me(self):
        if self.password != "spravne":
            raise NokturnoError("přihlášení se nepovedlo — zkontroluj e-mail a heslo")
        return {"is_premium": self.password == "spravne"}


class TestOvereniSledujteto(unittest.TestCase):
    def _check(self, nastaveni):
        r = router()
        r.st_api = FalesneSledujteto
        kousek = config.encode(config.from_mapping(nastaveni))
        return r.route(f"/c/{kousek}/check", ZAKLAD).data["sledujteto"]

    def test_premium(self):
        self.assertEqual(self._check({"st_email": "a@b.cz", "st_password": "spravne"}), {"ok": True, "premium": True})

    def test_spatne_heslo(self):
        vysledek = self._check({"st_email": "a@b.cz", "st_password": "spatne"})
        self.assertFalse(vysledek["ok"])

    def test_nevyplneno(self):
        self.assertIsNone(self._check({"ws_username": "u"}))

    def test_klice_projdou_do_jadra(self):
        options = config.from_mapping({"st_email": "a@b.cz", "st_password": "x"})
        self.assertEqual((options["st_email"], options["st_password"]), ("a@b.cz", "x"))


class TestStatistiky(unittest.TestCase):
    def test_zaznam_titulu_a_hlaseni_se_zdroji(self):
        from nokturno import statistiky as modul
        from nokturno.core.lib.stats import Stats
        odeslano = []
        puvodni = Stats.send
        Stats.send = lambda self, url, **kw: (odeslano.append(kw), (True, ""))[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                class Jadro:
                    store = type("Uloziste", (), {"dir": tmp})()

                    def sources(self):
                        return {"webshare": True, "luna": False, "sledujteto": True}

                    def meta(self, ctype, item_id):
                        return {"name": "Matrix", "year": 1999}, None

                modul.Statistiky("9.9").zpracuj(Jadro(), "movie", "tt0133093")
                hotovo = Stats(tmp).data
        finally:
            Stats.send = puvodni
        self.assertEqual(hotovo["plays"]["tt0133093"]["t"], "Matrix")
        self.assertEqual(odeslano[0]["product"], "stremio")
        self.assertEqual(set(odeslano[0]["sources"]), {"webshare", "sledujteto"})
        self.assertEqual(odeslano[0]["platform"], "Stremio")

    def test_zaznam_serialu_posila_nazev_serialu_ne_epizody(self):
        """2026-09-16: server slučuje statistiky podle normalizovaného názvu
        (`db.canonical_key`), takže skutečný (ne jen generický placeholder) název
        konkrétní epizody by rozštěpil sledovanost jednoho seriálu na tolik
        „titulů", kolik různých epizod se sledovalo. `_titul` proto musí vždy
        vrátit název seriálu, i když má epizoda vlastní netriviální název."""
        from nokturno import statistiky as modul
        from nokturno.core.lib.stats import Stats
        odeslano = []
        puvodni = Stats.send
        Stats.send = lambda self, url, **kw: (odeslano.append(kw), (True, ""))[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                class Jadro:
                    store = type("Uloziste", (), {"dir": tmp})()

                    def sources(self):
                        return {"webshare": True}

                    def meta(self, ctype, item_id):
                        return ({"name": "Lupin", "_title": "Lupin", "year": 2021},
                                {"title": "Skutečný název epizody, ne placeholder"})

                modul.Statistiky("9.9").zpracuj(Jadro(), "series", "tt123:1:2")
                hotovo = Stats(tmp).data
        finally:
            Stats.send = puvodni
        self.assertEqual(hotovo["plays"]["tt123:1:2"]["t"], "Lupin")

    def test_vypnuti_promennou(self):
        from nokturno.statistiky import Statistiky
        self.assertFalse(Statistiky.z_prostredi("1", {"NOKTURNO_STATS": "0"}).zapnuto)
        self.assertTrue(Statistiky.z_prostredi("1", {}).zapnuto)

    def test_router_zaznamena_zobrazene_streamy(self):
        r = router()
        volani = []
        r.statistiky = type("S", (), {"zaznamenej": lambda self, *a: volani.append(a)})()
        r.route(f"/c/{KOUSEK}/stream/movie/tt1.json", ZAKLAD)
        self.assertEqual(volani[0][1:], ("movie", "tt1", "stremio"))

    def test_router_predava_aplikaci_ze_user_agentu(self):
        r = router()
        volani = []
        r.statistiky = type("S", (), {"zaznamenej": lambda self, *a: volani.append(a)})()
        r.route(f"/c/{KOUSEK}/stream/movie/tt1.json", ZAKLAD, aplikace="nuvio")
        self.assertEqual(volani[0][1:], ("movie", "tt1", "nuvio"))

    def test_zaznam_posle_appku_dal_do_stats_send(self):
        from nokturno import statistiky as modul
        from nokturno.core.lib.stats import Stats
        odeslano = []
        puvodni = Stats.send
        Stats.send = lambda self, url, **kw: (odeslano.append(kw), (True, ""))[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                class Jadro:
                    store = type("Uloziste", (), {"dir": tmp})()

                    def sources(self):
                        return {}

                    def meta(self, ctype, item_id):
                        return {"name": "Matrix", "year": 1999}, None

                modul.Statistiky("9.9").zpracuj(Jadro(), "movie", "tt0133093", "streamlet")
        finally:
            Stats.send = puvodni
        self.assertEqual(odeslano[0]["client"], "streamlet")

    def test_klient_z_useragentu(self):
        from nokturno.routes import klient_z_useragent
        self.assertEqual(klient_z_useragent("Nuvio/0.8.9-beta"), "nuvio")
        self.assertEqual(klient_z_useragent("Streamlet/1.1.0 (android; …)"), "streamlet")
        self.assertEqual(klient_z_useragent("Stremio-Apple/0.5.1 (iPhone15,4; iOS 27.0)"), "stremio")
        self.assertEqual(klient_z_useragent(""), "stremio")
        self.assertEqual(klient_z_useragent(None), "stremio")
        self.assertEqual(klient_z_useragent("okhttp/5.3.2"), "stremio")
        self.assertEqual(klient_z_useragent("AIOStreams/2.34.1"), "stremio")


class TestZvukKodekKanaly(unittest.TestCase):
    def test_kanaly_z_hlavicky_jako_text_a_kodek(self):
        popis = {**POPIS, "langs": ["CZ", "EN"], "channels": {"CZ": "5.1", "EN": "2.0"},
                 "audio": [{"lang": "CZ", "channels": "5.1", "codec": "AC3"}, {"lang": "EN", "channels": "2.0", "codec": "AAC"}]}
        text = mapping.stream_object(popis, lambda u: u)["description"]
        self.assertIn("🇨🇿 5.1 AC3", text)
        self.assertIn("🇬🇧 2.0 AAC", text)

    def test_stopa_bez_jazyka(self):
        popis = {**POPIS, "langs": [], "channels": {}, "audio": [{"lang": "", "channels": "5.1", "codec": "EAC3"}]}
        self.assertIn("5.1 EAC3", mapping.stream_object(popis, lambda u: u)["description"])

    def test_sledujteto_odkaz_projde_prehranim(self):
        self.assertEqual(mapping.dekoduj(mapping.zakoduj("st:123")), "st:123")


class TestVlastniUloziste(unittest.TestCase):
    """Úložiště s heslem: heslo pošle přehrávač sám, z internetu ne na localhost."""

    def test_odkaz_projde_dekodovanim(self):
        self.assertEqual(mapping.dekoduj(mapping.zakoduj("dav:1:Filmy/a b.mkv")), "dav:1:Filmy/a b.mkv")

    def test_stream_nese_primou_adresu_a_heslo_v_proxyheaders(self):
        r = router()
        r.engine.file_request = lambda url: ("http://nas.lan/dav/Filmy/a.mkv", {"Authorization": "Basic x"})
        objekt = mapping.stream_object({"url": "dav:1:Filmy/a.mkv", "file": "a.mkv"},
                                       lambda v: "/play/" + v, primy=routes._primy(r.engine))
        self.assertEqual(objekt["url"], "http://nas.lan/dav/Filmy/a.mkv")
        self.assertEqual(objekt["behaviorHints"]["proxyHeaders"], {"request": {"Authorization": "Basic x"}})
        self.assertTrue(objekt["behaviorHints"]["notWebReady"])
        self.assertIn(mapping.JEN_V_APLIKACI, objekt["description"])

    def test_play_uz_uloziste_neobsluhuje(self):
        """Proxy zrušená v 5.2.26 — data tečou přímo, server se jich nedotkne."""
        odpoved = router().route("/play/" + mapping.zakoduj("dav:1:Filmy/a.mkv"), ZAKLAD)
        self.assertEqual(odpoved.status, 410)

    def test_nedostupny_soubor_se_vubec_nenabidne(self):
        """Bez hlaviček by stream stejně neodehrál — lepší ho neukázat než nabídnout mrtvý."""
        r = router()

        def spatne(url):
            raise NokturnoError("Tohle úložiště už není v nastavení.")
        r.engine.file_request = spatne
        objekt = mapping.stream_object({"url": "dav:3:a.mkv", "file": "a.mkv"},
                                       lambda v: "/play/" + v, primy=routes._primy(r.engine))
        self.assertIsNone(objekt)

    def test_tmdb_id_od_klienta_se_prelozi_na_imdb(self):
        """Nuvio posílá u titulů z TMDB katalogů `tmdb:<id>` — bez překladu vracel
        doplněk prázdno (zjištěno z logu 2026-09-18: `tmdb:37738` = Okresní přebor)."""
        class Tmdb:
            def __init__(self):
                self.dotazy = []

            def imdb_id(self, ctype, tmdb_id):
                self.dotazy.append((ctype, tmdb_id))
                return "tt1592598"

        r = router()
        r.engine.tmdb = Tmdb()
        r.route(f"/c/{KOUSEK}/stream/series/tmdb:37738:1:2.json", ZAKLAD)
        self.assertEqual(r.engine.tmdb.dotazy, [("series", "37738")])
        self.assertEqual(r.engine.dotazy[-1], ("series", "tt1592598:1:2"), "sezóna a díl musí zůstat")

        r.route(f"/c/{KOUSEK}/stream/movie/tmdb:37738.json", ZAKLAD)
        self.assertEqual(r.engine.dotazy[-1], ("movie", "tt1592598"))

    def test_tmdb_id_bez_klice_nebo_bez_imdb_vraci_prazdno(self):
        r = router()
        r.engine.tmdb = None                     # uživatel nemá vlastní klíč TMDB
        self.assertEqual(r.route(f"/c/{KOUSEK}/stream/movie/tmdb:1.json", ZAKLAD).data, {"streams": []})

        class BezImdb:
            def imdb_id(self, ctype, tmdb_id):
                return ""                        # TMDB titul zná, IMDb id nemá
        r.engine.tmdb = BezImdb()
        self.assertEqual(r.route(f"/c/{KOUSEK}/stream/movie/tmdb:1.json", ZAKLAD).data, {"streams": []})

    def test_vypis_streamu_vyda_primou_adresu(self):
        """Celá cesta `/stream/…`: úložiště se do odpovědi dostane jako přímá adresa
        zdroje, ostatní zdroje dál přes `/play/` (podepsaný odkaz platí jen chvíli)."""
        r = router(streamy=[{**POPIS, "url": "dav:1:Filmy/a.mkv", "file": "a.mkv"},
                            {**POPIS, "url": "ws:abc", "file": "b.mkv"}])
        r.engine.file_request = lambda url: ("http://nas.lan/dav/Filmy/a.mkv", {"Authorization": "Basic x"})
        streamy = r.route(f"/c/{KOUSEK}/stream/movie/tt1.json", ZAKLAD).data["streams"]
        self.assertEqual(streamy[0]["url"], "http://nas.lan/dav/Filmy/a.mkv")
        self.assertIn("proxyHeaders", streamy[0]["behaviorHints"])
        self.assertIn("/play/", streamy[1]["url"])
        self.assertNotIn("proxyHeaders", streamy[1]["behaviorHints"])

    def test_opakovana_selhani_prestanou_zkouset(self):
        """FastShare se při málo kreditu zkouší přihlásit znovu — u desítek souborů
        v jednom výpisu by to byla desítka síťových dotazů navíc."""
        pokusy = []

        class Engine:
            def file_request(self, url):
                pokusy.append(url)
                raise NokturnoError("nestačí kredit")

        primy = routes._primy(Engine())
        for i in range(10):
            self.assertIsNone(primy(f"fs:{i}:data1:10"))
        self.assertEqual(len(pokusy), routes.NEUSPECHU_DOST)

    def test_klice_projdou_nastavenim(self):
        options = config.from_mapping({"dav2_url": "https://nas/dav/", "dav2_username": "u",
                                       "dav2_password": "p", "dav2_name": "NAS", "dav9_url": "x"})
        self.assertEqual(options["dav2_url"], "https://nas/dav/")
        self.assertEqual(options["dav2_name"], "NAS")
        self.assertNotIn("dav9_url", options)
        prostredi = config.from_environ({"NOKTURNO_DAV1_URL": "http://nas/", "NOKTURNO_DAV1_PASSWORD": "p"})
        self.assertEqual((prostredi["dav1_url"], prostredi["dav1_password"]), ("http://nas/", "p"))

    def test_z_internetu_ne_na_tenhle_stroj_ani_do_site(self):
        """Z internetu jen veřejné adresy: localhost, metadata cloudu, domácí síť i tailnet
        (100.64/10) ven. Uživatel zvenku na naši LAN stejně nedosáhne — přes doplněk by
        sahal jen na CoreELEC, Home Assistant a dashboard (2026-09-14)."""
        adresy = {"localhost": ["127.0.0.1"], "meta": ["169.254.169.254"], "nas.lan": ["192.168.1.241"],
                  "nokturno.ts.net": ["100.125.137.18"], "v6": ["::1"], "cloud.example": ["93.184.216.34"],
                  "mapped": ["::ffff:10.0.0.5"]}
        options = {f"dav{i}_url": f"http://{h}:8090/"
                   for i, h in enumerate(("localhost", "cloud.example", "nas.lan"), 1)}
        options["dav1_password"] = "tajne"
        cista = config.bez_lokalnich_uloziste(options, resolve=lambda h: adresy[h])
        self.assertEqual(sorted(k for k in cista if k.startswith("dav")), ["dav2_url"])
        for host in ("nokturno.ts.net", "meta", "v6", "mapped"):
            self.assertNotIn("dav1_url", config.bez_lokalnich_uloziste({"dav1_url": f"https://{host}:10000/"},
                                                                       resolve=lambda h: adresy[h]), host)
        self.assertNotIn("dav1_url", config.bez_lokalnich_uloziste({"dav1_url": "http://neexistuje/"},
                                                                   resolve=lambda h: []))

    def test_verejny_pozadavek_localhost_nedostane(self):
        kousek = config.encode(config.from_mapping({"ws_username": "u", "ws_password": "p",
                                                    "dav1_url": "http://127.0.0.1:8080/"}))
        r = router()
        r.route(f"/c/{kousek}/stream/movie/tt1.json", ZAKLAD, verejny=True)
        self.assertNotIn("dav1_url", r.enginy_test.pozadovana_nastaveni[-1])
        r.route(f"/c/{kousek}/stream/movie/tt1.json", ZAKLAD, verejny=False)
        self.assertEqual(r.enginy_test.pozadovana_nastaveni[-1]["dav1_url"], "http://127.0.0.1:8080/")

    def test_overeni_uloziste(self):
        class Falesne:
            def __init__(self, url, user, password, name="", slot=1):
                self.password = password

            def check(self):
                if self.password != "tajne":
                    raise Exception("špatné jméno nebo heslo")
                return 2
        r = router()
        r.dav_api = Falesne
        data = r.check({"dav1_url": "http://nas/", "dav1_password": "tajne",
                        "dav3_url": "http://nas2/", "dav3_password": "x"}).data["uloziste"]
        self.assertEqual(data, [{"slot": 1, "ok": True, "polozek": 2},
                                {"slot": 3, "ok": False, "chyba": "špatné jméno nebo heslo"}])

    def test_formular_ma_tri_uloziste(self):
        html = router().route("/configure", ZAKLAD).html
        for i in (1, 2, 3):
            self.assertIn(f'name="dav{i}_url"', html)
        self.assertNotIn("__ULOZISTE__", html)


class TestSlovencina(unittest.TestCase):
    """Úvod a formulář slovensky: `?lang=` má přednost, jinak Accept-Language, jinak čeština."""

    def html(self, cesta, jazyk=None):
        return router().route(cesta, ZAKLAD, jazyk=jazyk).html

    @staticmethod
    def jmena_poli(html):
        import re
        return set(re.findall(r'name="([^"]+)"', html)) - {"viewport", "description"}

    def test_parametr_lang_da_slovenstinu(self):
        uvod = self.html("/?lang=sk")
        self.assertIn('lang="sk"', uvod)
        self.assertIn("Kde Nokturno beží", uvod)
        formular = self.html(f"/c/{KOUSEK}/configure?lang=sk")
        self.assertIn('<html lang="sk">', formular)
        self.assertIn("U WebShare chýba heslo.", formular)
        self.assertIn('"uzivatel"', formular, "předvyplnění funguje i slovensky")

    def test_parametr_lang_prebije_hlavicku(self):
        self.assertIn('<html lang="cs">', self.html("/configure?lang=cs", jazyk="sk"))
        self.assertIn('<html lang="sk">', self.html("/configure?lang=sk", jazyk="cs"))

    def test_accept_language(self):
        from nokturno.routes import jazyk_z_hlavicky
        self.assertEqual(jazyk_z_hlavicky("sk-SK,sk;q=0.9"), "sk")
        self.assertEqual(jazyk_z_hlavicky("sk"), "sk")
        self.assertEqual(jazyk_z_hlavicky("en;q=0.5, sk;q=0.8"), "sk", "rozhoduje q, ne pořadí")
        for hlavicka in ("cs", "cs-CZ,cs;q=0.9,sk;q=0.8", "en-US,en;q=0.9", "", None, "sk;q=0, en", "rozbite;;q=x"):
            self.assertEqual(jazyk_z_hlavicky(hlavicka), "cs", hlavicka)
        self.assertIn('<html lang="sk">', self.html("/configure", jazyk=jazyk_z_hlavicky("sk-SK,sk;q=0.9")))
        for jazyk in ("cs", "en", None):
            self.assertIn('<html lang="cs">', self.html("/configure", jazyk=jazyk), jazyk)
            self.assertIn('lang="cs"', self.html("/", jazyk=jazyk), jazyk)

    def test_bez_hlavicky_i_parametru_cestina(self):
        # dosavadní volání bez `jazyk`
        self.assertIn('<html lang="cs">', router().route("/configure", ZAKLAD).html)

    def test_zastupne_symboly_nahrazene(self):
        for cesta in ("/?lang=sk", "/configure?lang=sk", f"/c/{KOUSEK}/configure?lang=sk"):
            html = self.html(cesta)
            for symbol in ("__ZAKLAD__", "__VERZE__", "__NASTAVENI__"):
                self.assertNotIn(symbol, html, (cesta, symbol))
            self.assertIn(ZAKLAD, html)

    def test_slovensky_formular_ma_tataz_pole(self):
        cs, sk = self.html("/configure?lang=cs"), self.html("/configure?lang=sk")
        self.assertTrue(self.jmena_poli(cs))
        self.assertEqual(self.jmena_poli(cs), self.jmena_poli(sk),
                         "každá změna české stránky se musí promítnout i do configure.sk.html")
        for n in (1, 2, 3):
            for pole in ("url", "username", "password", "name"):
                self.assertIn(f'name="dav{n}_{pole}"', sk)

    def test_adresa_doplnku_nenese_jazyk(self):
        for jazyk in ("cs", "sk"):
            html = self.html(f"/configure?lang={jazyk}")
            self.assertIn('const adresa = () => ZAKLAD + "/c/" + kousek() + "/manifest.json";', html)
            self.assertIn(f'const ZAKLAD = "{ZAKLAD}";', html)

    def test_manifest_zustava_cesky_a_lang_ho_nerozbije(self):
        r = router()
        self.assertEqual(r.route(f"/c/{KOUSEK}/manifest.json?lang=sk", ZAKLAD).data,
                         r.route(f"/c/{KOUSEK}/manifest.json", ZAKLAD).data)

    def test_chybejici_slovenska_stranka_spadne_na_ceskou(self):
        from nokturno import routes
        puvodni = routes.STATIKA
        with tempfile.TemporaryDirectory() as adresar:
            (pathlib.Path(adresar) / "index.html").write_text('<html lang="cs">__ZAKLAD__', encoding="utf-8")
            routes.STATIKA = pathlib.Path(adresar)
            try:
                self.assertEqual(self.html("/?lang=sk"), f'<html lang="cs">{ZAKLAD}')
            finally:
                routes.STATIKA = puvodni


if __name__ == "__main__":
    unittest.main()


class TestVerejnaSit(unittest.TestCase):
    """Požadavek z internetu se smí připojit jen na veřejné adresy — hlídá se až
    při navázání spojení, ne podle jména v nastavení (přesměrování, DNS rebinding)."""

    def test_zakazane_adresy(self):
        from nokturno import sit
        for a in ("127.0.0.1", "::1", "0.0.0.0", "169.254.169.254", "10.1.2.3", "172.16.0.1", "192.168.1.21",
                  "100.100.100.100", "224.0.0.1", "fe80::1", "fd00::1", "::ffff:192.168.1.5", "nesmysl"):
            self.assertTrue(sit.zakazana(a), a)
        for a in ("93.184.216.34", "1.1.1.1", "2606:4700:4700::1111", "100.63.255.255", "100.128.0.1"):
            self.assertFalse(sit.zakazana(a), a)

    def test_opener_odmitne_spojeni_dovnitr(self):
        """Server na 127.0.0.1 je z internetu zakázaný, i když ho DNS nebo přesměrování podstrčí."""
        import threading
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from nokturno import sit

        class Zdroj(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Zdroj)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/a.mkv"
        try:
            with self.assertRaises(urllib.error.URLError) as ctx:
                sit.OPENER.open(url, timeout=5)
            self.assertIsInstance(ctx.exception.reason, sit.ChybaCile)
            with urllib.request.urlopen(url, timeout=5) as resp:
                self.assertEqual(resp.read(), b"ok", "domácí požadavek jde výchozím openerem dál")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_presmerovani_mimo_http_se_nesleduje(self):
        from nokturno import sit
        handler = sit._JenHttp()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "ftp://192.168.1.1/x"))
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "file:///etc/passwd"))

    def test_verejne_jadro_ma_hlidany_opener_domaci_ne(self):
        from nokturno import sit
        from nokturno.enginy import Enginy
        enginy = Enginy(tempfile.mkdtemp(), {"hs_enabled": True})
        options = config.from_mapping({"ws_username": "u", "ws_password": "p"})
        verejne = enginy.pro(options, verejny=True)
        domaci = enginy.pro(options)
        self.assertIsNot(verejne, domaci)
        self.assertIs(verejne.opener, sit.OPENER)
        self.assertIsNone(domaci.opener)
        self.assertIs(enginy.pro(options, verejny=True), verejne)
        self.assertEqual(len(enginy), 2)

    def test_verejne_jadro_ma_stropy_na_cizi_uloziste(self):
        """Adresa WebDAV je v nastavení doplňku — kdokoli ji může nasměrovat na server,
        který každý PROPFIND drží a vrací stále nové podsložky (audit, nález 5)."""
        from nokturno.core.lib import storage_api
        from nokturno.enginy import Enginy
        enginy = Enginy(tempfile.mkdtemp(), {})
        options = config.from_mapping({"dav1_url": "https://uloziste.example/dav/"})
        verejne = enginy.pro(options, verejny=True)
        domaci = enginy.pro(options)
        self.assertEqual(verejne.storage_limits["crawl_deadline"], storage_api.PUBLIC_CRAWL_DEADLINE)
        self.assertEqual(domaci.storage_limits, {}, "vlastní NAS doma strop nemá")
        dav = verejne.storages[0]
        self.assertEqual((dav.crawl_deadline, dav.max_dirs, dav.timeout),
                         (storage_api.PUBLIC_CRAWL_DEADLINE, storage_api.PUBLIC_MAX_DIRS,
                          storage_api.PUBLIC_TIMEOUT))
        self.assertEqual(domaci.storages[0].max_dirs, storage_api.MAX_DIRS)

    def test_klic_tmdb_dostane_i_verejne_jadro(self):
        """Vědomá výjimka z „veřejný požadavek nedostane nastavení z prostředí":
        bez klíče TMDB nejde přeložit `tmdb:` id od klientů, a tím ani najít streamy
        k titulu z TMDB katalogu. Klíč je zdarma a jen na čtení, účty chráněné dál."""
        from nokturno.enginy import Enginy
        enginy = Enginy(tempfile.mkdtemp(), {}, tmdb_key="klic-instance")
        options = config.from_mapping({"ws_username": "u", "ws_password": "p"})
        for verejny in (True, False):
            self.assertEqual(enginy.pro(options, verejny=verejny).options["tmdb_api_key"], "klic-instance")
        self.assertNotIn("tmdb_api_key", options, "do nastavení z adresy se klíč nepromítne")
        bez = Enginy(tempfile.mkdtemp(), {})
        self.assertNotIn("tmdb_api_key", bez.pro(options).options)

    def test_nejdele_nepouzite_jadro_vypadne(self):
        """Limit drží paměť na uzdě, ale musí vyhodit opravdu to nejdéle nepoužité —
        jinak by se jádro právě obsluhovaného uživatele zahodilo zpod ruky."""
        from nokturno.enginy import Enginy
        enginy = Enginy(tempfile.mkdtemp(), {}, limit=2, nova_limit=2)
        prvni = enginy.pro(config.from_mapping({"ws_username": "a"}))
        druhe = enginy.pro(config.from_mapping({"ws_username": "b"}))
        self.assertIs(enginy.pro(config.from_mapping({"ws_username": "a"})), prvni, "sáhnutí ho omladí")
        enginy.pro(config.from_mapping({"ws_username": "c"}))       # přeteče → padá `druhe`
        self.assertEqual(len(enginy), 2)
        self.assertIs(enginy.pro(config.from_mapping({"ws_username": "a"})), prvni)
        self.assertIsNot(enginy.pro(config.from_mapping({"ws_username": "b"})), druhe)

    def test_bot_s_vymyslenymi_nastavenimi_nevytlaci_overena_jadra(self):
        """Nové jádro je na zkoušku; do ověřených ho pustí až první vrácený stream (`povysit`).
        Bot, který tvoří nastavení bez konce, tak vytlačuje jen jiná nová jádra."""
        from nokturno.enginy import Enginy
        enginy = Enginy(tempfile.mkdtemp(), {}, limit=2, nova_limit=3, nova_jadra=(10 ** 6, 3600))
        uzivatele = [config.from_mapping({"ws_username": u}) for u in ("a", "b")]
        jadra = [enginy.pro(o) for o in uzivatele]
        for o in uzivatele:
            enginy.povysit(o)
        for i in range(50):
            enginy.pro(config.from_mapping({"ws_username": f"bot{i}"}))
        self.assertEqual(len(enginy), 2 + 3)                       # 2 ověřená + 3 nejnovější bota
        self.assertIs(enginy.pro(uzivatele[0]), jadra[0])
        self.assertIs(enginy.pro(uzivatele[1]), jadra[1])

    def test_povyseni_prenese_jadro_do_overenych_a_je_idempotentni(self):
        from nokturno.enginy import Enginy
        enginy = Enginy(tempfile.mkdtemp(), {}, limit=5, nova_limit=1)
        o = config.from_mapping({"ws_username": "a"})
        jadro = enginy.pro(o)
        enginy.povysit(o)
        enginy.povysit(o)
        enginy.pro(config.from_mapping({"ws_username": "b"}))
        enginy.pro(config.from_mapping({"ws_username": "c"}))       # vytlačí jen `b`
        self.assertIs(enginy.pro(o), jadro)

    def test_router_povysi_jadro_az_po_prvnim_streamu(self):
        r = router()
        volani = []
        r.enginy.povysit = lambda options, verejny=False: volani.append(options)
        r.route(f"/c/{KOUSEK}/manifest.json", ZAKLAD)
        self.assertEqual(volani, [])
        odp = r.route(f"/c/{KOUSEK}/stream/movie/tt0133093.json", ZAKLAD)
        self.assertEqual(odp.status, 200)
        self.assertEqual(len(volani), 1 if odp.data and odp.data.get("streams") else 0)

    def test_limit_jader_pokryva_bezny_soubeh(self):
        """2026-09-17: za 24 h 155 různých nastavení proti limitu 20 — jádra se protáčela
        (439 vzniků za den, pokaždé nové přihlášení ke zdrojům a studená cache)."""
        from nokturno import enginy as modul
        self.assertGreaterEqual(modul.LIMIT, 50)

    def test_router_zaklada_verejne_jadro_pro_pozadavek_z_internetu(self):
        r = router()
        r.route(f"/c/{KOUSEK}/stream/movie/tt1.json", ZAKLAD, verejny=True)
        self.assertTrue(r.enginy_test.verejny)
        r.route(f"/c/{KOUSEK}/stream/movie/tt1.json", ZAKLAD)
        self.assertFalse(r.enginy_test.verejny)

    def test_overeni_uloziste_zvenku_nehlasi_detail(self):
        """„connection refused" vs. „timed out" u adres v naší síti by z tlačítka udělalo skener portů."""
        class Falesne:
            def __init__(self, url, user, password, name="", slot=1, opener=None):
                self.opener = opener

            def check(self):
                raise Exception("[Errno 111] Connection refused")
        r = router()
        r.dav_api = Falesne
        zvenku = r.check({"dav1_url": "http://cokoli/"}, verejny=True).data["uloziste"][0]
        self.assertEqual(zvenku, {"slot": 1, "ok": False, "chyba": "nedostupné"})
        doma = r.check({"dav1_url": "http://cokoli/"}).data["uloziste"][0]
        self.assertIn("Connection refused", doma["chyba"])


class TestFormularBezCizihoSkriptu(unittest.TestCase):
    """Hodnoty z adresy jdou do `<script>` formuláře — `</script>` ve jménu účtu by
    ukončilo skript a zbytek by prohlížeč spustil (stránka sbírá hesla)."""

    def test_nastaveni_do_scriptu_je_escapovane(self):
        zly = "</script><script>alert(document.domain)</script>"
        kousek = config.encode(config.from_mapping({"ws_username": zly, "ws_password": "p"}))
        html = router().route(f"/c/{kousek}/configure", ZAKLAD).html
        self.assertNotIn("</script><script>alert", html)
        self.assertIn("\\u003c/script\\u003e", html)
        # a JSON zůstává čitelný — JavaScript escapované znaky přečte jako tentýž řetězec
        import json
        zacatek = html.index("const soucasne = ") + len("const soucasne = ")
        self.assertEqual(json.loads(html[zacatek:html.index(";", zacatek)])["ws_username"], zly)

    def test_zaklad_je_escapovany(self):
        html = router().route("/configure", 'http://x"><script>').html
        self.assertNotIn('"><script>', html)
        self.assertIn("http://x&quot;&gt;&lt;script&gt;", html)

    def test_stranky_maji_ochranne_hlavicky(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from nokturno.routes import Odpoved
        from nokturno.server import Handler

        class Smerovac:
            def route(self, cesta, zaklad, verejny=False, jazyk=None, klient="", aplikace="stremio"):
                return Odpoved(html="<p>x</p>") if cesta.endswith("/configure") else Odpoved(data={"ok": True})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.router = Smerovac()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        zaklad = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{zaklad}/c/abc/configure", timeout=5) as resp:
                h = resp.headers
                self.assertIn("default-src 'none'", h["Content-Security-Policy"])
                self.assertEqual(h["X-Frame-Options"], "DENY")
                self.assertEqual(h["Referrer-Policy"], "no-referrer")
                self.assertEqual(h["Cache-Control"], "no-store")
                self.assertNotIn("Python", h["Server"])
            with urllib.request.urlopen(f"{zaklad}/health", timeout=5) as resp:
                self.assertIsNone(resp.headers["Content-Security-Policy"], "JSON hlavičky stránek nepotřebuje")
                self.assertIsNone(resp.headers["Cache-Control"])
        finally:
            srv.shutdown()
            srv.server_close()


class TestFormularHellSpyAJazyk(unittest.TestCase):
    """Odškrtnutý checkbox dřív do adresy nešel a server dosadil výchozí „zapnuto" —
    HellSpy šlo zapnout, ale ne vypnout; „nezáleží" u jazyka končilo jako čeština."""

    def test_server_bere_false_a_any(self):
        self.assertIs(config.from_mapping({"hs_enabled": False})["hs_enabled"], False)
        self.assertIs(config.from_mapping({})["hs_enabled"], True, "bez klíče zůstává výchozí")
        self.assertEqual(config.from_mapping({"pref_lang": "ANY"})["pref_lang"], "")
        self.assertEqual(config.from_mapping({})["pref_lang"], "CZ")
        self.assertEqual(config.from_mapping({"pref_lang": "HU"})["pref_lang"], "HU")
        # jak to pošle formulář: False přežije encode, ANY se rozklíčuje na prázdné
        odesle = {"ws_username": "u", "ws_password": "p", "hs_enabled": False, "pref_lang": "ANY"}
        options = config.decode(config.encode(odesle))
        self.assertIs(options["hs_enabled"], False)
        self.assertEqual(options["pref_lang"], "")

    def test_formular_posila_checkbox_vzdy_a_nezalezi_jako_any(self):
        for jmeno in ("configure.html", "configure.sk.html"):
            html = (pathlib.Path(__file__).resolve().parent.parent / "nokturno" / "static" / jmeno).read_text(encoding="utf-8")
            self.assertIn("out[pole.name] = pole.checked", html, jmeno)
            self.assertNotIn('if (pole.checked) out[pole.name] = true', html, jmeno)
            self.assertIn('<option value="ANY">', html, jmeno)
            self.assertNotIn('<option value="">', html, jmeno)
            self.assertIn('hodnota === "") pole.value = "ANY"', html, jmeno)


class TestLimityAUklid(unittest.TestCase):
    def test_check_ma_limit_na_adresu(self):
        from nokturno.routes import Okno
        r = router()
        r.ws_api = FalesnyWebshare
        r.check_okno = Okno(2, 300)
        kousek = config.encode(config.from_mapping({"ws_username": "u", "ws_password": "spravne"}))
        for _ in range(2):
            self.assertEqual(r.route(f"/c/{kousek}/check", ZAKLAD, klient="1.2.3.4").status, 200)
        self.assertEqual(r.route(f"/c/{kousek}/check", ZAKLAD, klient="1.2.3.4").status, 429)
        self.assertEqual(r.route(f"/c/{kousek}/check", ZAKLAD, klient="5.6.7.8").status, 200, "jiná adresa jede")
        self.assertEqual(r.route(f"/c/{kousek}/manifest.json", ZAKLAD, klient="1.2.3.4").status, 200,
                         "limit platí jen na /check")

    def test_ucty_z_adresy_nejdou_do_logu(self):
        from nokturno.server import bezpecna_cesta
        cesta = bezpecna_cesta(f"/c/{KOUSEK}/stream/movie/tt1.json")
        self.assertNotIn(KOUSEK, cesta)
        self.assertNotIn("uzivatel", cesta)
        self.assertEqual(cesta, f"/c/{config.fingerprint(NASTAVENI)}/stream/movie/tt1.json")
        self.assertEqual(bezpecna_cesta("/c/nesmysl!!/x"), "/c/?/x")
        self.assertEqual(bezpecna_cesta("/health"), "/health")
        self.assertEqual(bezpecna_cesta(f"/c/{KOUSEK}/configure?lang=sk"), f"/c/{config.fingerprint(NASTAVENI)}/configure?lang=sk")

    def test_blokovany_otisk_dostane_403(self):
        otisk = config.fingerprint(NASTAVENI)
        enginy = FalesneEnginy(FalesnyEngine())
        r = Router(enginy, blokovane={otisk})
        odp = r.route(f"/c/{KOUSEK}/manifest.json", ZAKLAD)
        self.assertEqual(odp.status, 403)

    def test_limit_streamu_na_ip(self):
        from nokturno import routes
        r = router()
        r.stream_okno = routes.Okno(3, 600)
        cesta = f"/c/{KOUSEK_HS}/stream/movie/tt0133093.json"
        self.assertEqual([r.route(cesta, ZAKLAD, klient="1.2.3.4").status for _ in range(4)],
                         [200, 200, 200, 429])
        # stejné nastavení z jiné IP limit nesdílí (nastavení bez účtů má spousta lidí)
        self.assertEqual(r.route(cesta, ZAKLAD, klient="5.6.7.8").status, 200)
        jina = config.encode(config.from_mapping({"hs_enabled": True, "pref_lang": "SK"}))
        self.assertEqual(r.route(f"/c/{jina}/stream/movie/tt0133093.json", ZAKLAD, klient="1.2.3.4").status, 429)
        # vlastní účty = jedinečný otisk: limit se počítá na něj, ne na sdílenou IP
        ucty = f"/c/{KOUSEK}/stream/movie/tt0133093.json"
        self.assertEqual([r.route(ucty, ZAKLAD, klient="1.2.3.4").status for _ in range(4)], [200, 200, 200, 429])

    def test_limit_streamu_ipv6_po_64(self):
        from nokturno import routes
        r = router()
        r.stream_okno = routes.Okno(2, 600)
        cesta = f"/c/{KOUSEK_HS}/stream/movie/tt0133093.json"
        stavy = [r.route(cesta, ZAKLAD, klient=f"2a09:bac1:1da0:10::{i}").status for i in range(3)]
        self.assertEqual(stavy, [200, 200, 429])
        self.assertEqual(r.route(cesta, ZAKLAD, klient="2a09:bac1:1da0:11::1").status, 200)

    def test_opakovane_narazeni_na_limit_zablokuje_adresu_na_hodinu(self):
        from nokturno import routes
        r = router()
        r.stream_okno = routes.Okno(2, 600)
        r.blokace = routes.Blokace(prah=3, okno_s=600, doba_s=3600)
        cesta = f"/c/{KOUSEK_HS}/stream/movie/tt0133093.json"
        stavy = [r.route(cesta, ZAKLAD, klient="1.2.3.4").status for _ in range(7)]
        # 2× 200, pak 3× 429 a od třetího odmítnutí už rovnou 403 (blokace)
        self.assertEqual(stavy, [200, 200, 429, 429, 429, 403, 403])
        odp = r.route(cesta, ZAKLAD, klient="1.2.3.4")
        self.assertEqual(odp.utok[0], "auto-blok")
        # jiná adresa a jiné cesty téže adresy zůstávají dostupné
        self.assertEqual(r.route(cesta, ZAKLAD, klient="5.6.7.8").status, 200)
        self.assertEqual(r.route(f"/c/{KOUSEK_HS}/manifest.json", ZAKLAD, klient="1.2.3.4").status, 200)

    def test_blokace_vyprsi(self):
        from unittest import mock
        from nokturno import routes
        b = routes.Blokace(prah=2, okno_s=600, doba_s=100)
        b.prohresek("1.2.3.4")
        self.assertTrue(b.prohresek("1.2.3.4"))
        self.assertTrue(b.blokovana("1.2.3.4"))
        with mock.patch.object(routes.time, "time", return_value=time.time() + 101):
            self.assertFalse(b.blokovana("1.2.3.4"))

    def test_nova_jadra_z_jedne_adresy_maji_strop_existujici_ne(self):
        from nokturno.enginy import Enginy, PrilisMnohoNovych
        with tempfile.TemporaryDirectory() as tmp:
            enginy = Enginy(tmp, nova_jadra=(2, 3600))
            a, b, c = (config.from_mapping({"ws_username": u}) for u in "abc")
            prvni = enginy.pro(a, klient="1.2.3.4")
            enginy.pro(b, klient="1.2.3.4")
            with self.assertRaises(PrilisMnohoNovych):
                enginy.pro(c, klient="1.2.3.4")
            self.assertIs(enginy.pro(a, klient="1.2.3.4"), prvni)      # existující jádro se neomezuje
            enginy.pro(c, klient="5.6.7.8")                            # jiná adresa má vlastní strop
            self.assertEqual(len(enginy), 3)

    def test_router_vrati_429_kdyz_adresa_zaklada_moc_jader(self):
        from nokturno.enginy import PrilisMnohoNovych
        r = router()
        r.enginy.pro = lambda *a, **k: (_ for _ in ()).throw(PrilisMnohoNovych("x"))
        odp = r.route(f"/c/{KOUSEK}/stream/movie/tt0133093.json", ZAKLAD, klient="1.2.3.4")
        self.assertEqual(odp.status, 429)
        self.assertEqual(odp.utok[0], "limit")

    def test_identita_vydani_a_overeni(self):
        from nokturno.identita import Identita
        i = Identita("tajne")
        t = i.vydat()
        self.assertTrue(i.platna(t))
        self.assertFalse(i.platna(t[:-1] + ("0" if t[-1] != "0" else "1")))
        self.assertFalse(i.platna("nesmysl"))
        self.assertFalse(Identita("jine").platna(t))
        vyp = Identita("")
        self.assertEqual(vyp.vydat(), "")
        self.assertFalse(vyp.platna(t))
        self.assertEqual(config.from_mapping({"id": t, "ws_username": "a"})["id"], t)
        self.assertNotIn("id", config.from_mapping({"id": "../x"}))
        self.assertNotEqual(config.fingerprint(config.from_mapping({"id": t})),
                            config.fingerprint(config.from_mapping({"id": i.vydat()})))

    def test_neplatna_identita_v_adrese_je_403_krome_formulare(self):
        from nokturno.identita import Identita
        r = router()
        r.identita = Identita("tajne")
        spatne = config.encode(config.from_mapping({**NASTAVENI, "id": "0" * 16 + "." + "0" * 16}))
        odp = r.route(f"/c/{spatne}/stream/movie/tt0133093.json", ZAKLAD, klient="1.2.3.4")
        self.assertEqual(odp.status, 403)
        self.assertEqual(odp.utok[0], "neplatné id")
        self.assertEqual(r.route(f"/c/{spatne}/configure", ZAKLAD, klient="1.2.3.4").status, 200)
        dobre = config.encode(config.from_mapping({**NASTAVENI, "id": r.identita.vydat()}))
        self.assertEqual(r.route(f"/c/{dobre}/stream/movie/tt0133093.json", ZAKLAD, klient="1.2.3.4").status, 200)

    def test_bez_tajemstvi_se_identita_ignoruje(self):
        r = router()
        s = config.encode(config.from_mapping({**NASTAVENI, "id": "a" * 16 + "." + "b" * 16}))
        self.assertEqual(r.route(f"/c/{s}/stream/movie/tt0133093.json", ZAKLAD).status, 200)
        self.assertNotIn("id", r.enginy_test.pozadovana_nastaveni[-1])

    def test_limity_s_identitou_jdou_na_uzivatele_ne_na_adresu(self):
        from nokturno import routes
        from nokturno.identita import Identita
        r = router()
        r.identita = Identita("tajne")
        r.stream_okno = routes.Okno(2, 600)
        a = config.encode(config.from_mapping({**NASTAVENI, "id": r.identita.vydat()}))
        b = config.encode(config.from_mapping({**NASTAVENI, "id": r.identita.vydat()}))
        cesta = lambda k: f"/c/{k}/stream/movie/tt0133093.json"   # noqa: E731
        self.assertEqual([r.route(cesta(a), ZAKLAD, klient="1.2.3.4").status for _ in range(3)], [200, 200, 429])
        # jiná identita ze stejné adresy má vlastní limit; stejná identita z jiné adresy limit sdílí
        self.assertEqual(r.route(cesta(b), ZAKLAD, klient="1.2.3.4").status, 200)
        self.assertEqual(r.route(cesta(a), ZAKLAD, klient="9.9.9.9").status, 429)

    def test_formular_vlozi_jen_stavajici_identitu(self):
        from nokturno.identita import Identita
        r = router()
        r.identita = Identita("tajne")
        html = r.route("/configure", ZAKLAD, klient="1.2.3.4").html
        self.assertIn('form.elements["id"].value = ""', html)      # novou si stránka vyžádá za důkaz práce
        t1 = r.identita.vydat()
        s = config.encode(config.from_mapping({**NASTAVENI, "id": t1}))
        html = r.route(f"/c/{s}/configure", ZAKLAD, klient="1.2.3.4").html
        self.assertIn(f'.value = "{t1}"', html)

    def test_identita_se_vyda_jen_za_dukaz_prace_a_omezene(self):
        from nokturno import routes
        from nokturno.identita import Identita, najdi_reseni
        r = router()
        r.identita = Identita("tajne")
        r.identita.bity = 8
        r.id_okno = routes.Okno(2, 3600)
        vyzva = r.route("/identita/vyzva", ZAKLAD, klient="1.2.3.4").data["vyzva"]
        self.assertTrue(vyzva)
        self.assertEqual(r.route(f"/identita?vyzva={vyzva}&reseni=nesmysl", ZAKLAD, klient="1.2.3.4").status, 403)
        reseni = najdi_reseni(vyzva, 8)
        # výzva je vázaná na adresu klienta: z jiné adresy neplatí (audit 2026-09-19)
        self.assertEqual(r.route(f"/identita?vyzva={vyzva}&reseni={reseni}", ZAKLAD, klient="9.9.9.9").status, 403)
        odp = r.route(f"/identita?vyzva={vyzva}&reseni={reseni}", ZAKLAD, klient="1.2.3.4")
        self.assertEqual(odp.status, 200)
        self.assertTrue(r.identita.platna(odp.data["id"]))
        # tatáž vyřešená výzva podruhé neprojde (replay)
        self.assertEqual(r.route(f"/identita?vyzva={vyzva}&reseni={reseni}", ZAKLAD, klient="1.2.3.4").status, 403)
        # cizí/upravená výzva neprojde, vypršelá taky
        cizi = Identita("jine").vyzva("1.2.3.4")
        self.assertFalse(r.identita.over_dukaz(cizi, najdi_reseni(cizi, 8), "1.2.3.4"))
        stara = r.identita.vyzva("1.2.3.4", now=time.time() - 3600)
        self.assertFalse(r.identita.over_dukaz(stara, najdi_reseni(stara, 8), "1.2.3.4"))
        # limit vydávání na adresu (2. projde, 3. ne) — i se správným důkazem
        v2 = r.identita.vyzva("1.2.3.4")
        self.assertEqual(r.route(f"/identita?vyzva={v2}&reseni={najdi_reseni(v2, 8)}", ZAKLAD, klient="1.2.3.4").status, 200)
        v3 = r.identita.vyzva("1.2.3.4")
        self.assertEqual(r.route(f"/identita?vyzva={v3}&reseni={najdi_reseni(v3, 8)}", ZAKLAD, klient="1.2.3.4").status, 429)
        self.assertEqual(router().route("/identita/vyzva", ZAKLAD).data["vyzva"], "")   # bez tajemství

    def test_identita_ma_platnost_a_formular_ji_obnovi(self):
        """Audit 2026-09-19: token neměl čas vydání a platil navždy."""
        from nokturno import identita as mod
        from nokturno.identita import Identita
        i = Identita("tajne")
        now = time.time()
        t = i.vydat(now=now)
        self.assertTrue(i.platna(t, now=now))
        self.assertTrue(i.platna(t, now=now + mod.PLATNOST - 1))
        self.assertFalse(i.platna(t, now=now + mod.PLATNOST + 1))
        self.assertFalse(i.platna(t, now=now - 3600), "token z budoucnosti neplatí")
        self.assertFalse(i.k_obnove(t, now=now + 10))
        self.assertTrue(i.k_obnove(t, now=now + mod.PLATNOST * 0.6))
        # starý tvar (6.1.0–6.1.2) platí jen do STARE_DO a formulář ho vymění
        nahoda = "a" * 16
        stary = f"{nahoda}.{i._podpis(nahoda)}"
        self.assertTrue(i.platna(stary, now=mod.STARE_DO - 1))
        self.assertFalse(i.platna(stary, now=mod.STARE_DO + 1))
        self.assertTrue(i.k_obnove(stary, now=mod.STARE_DO - 1))
        self.assertTrue(config.ID_RE.match(t) and config.ID_RE.match(stary))
        r = router()
        r.identita = Identita("tajne")
        s = config.encode(config.from_mapping({**NASTAVENI, "id": stary}))
        html = r.route(f"/c/{s}/configure", ZAKLAD, klient="1.2.3.4").html
        self.assertNotIn(f'.value = "{stary}"', html)
        novy = html.split('form.elements["id"].value = "', 1)[1].split('"', 1)[0]
        self.assertTrue(r.identita.platna(novy) and r.identita.vydana(novy) is not None)
        # starý tvar dál funguje na /stream
        self.assertEqual(r.route(f"/c/{s}/stream/movie/tt0133093.json", ZAKLAD, klient="1.2.3.4").status, 200)

    def test_identita_neobejde_strop_na_adresu(self):
        """Audit 2026-09-19: N identit z jedné adresy = N × limit. Nad limitem na uživatele je
        strop na adresu (IPv6 po /64), který identita neobejde."""
        from nokturno import routes
        from nokturno.identita import Identita
        r = router()
        r.identita = Identita("tajne")
        r.stream_okno = routes.Okno(2, 600)
        r.ip_okno = routes.Okno(3, 600)
        cesta = lambda k: f"/c/{k}/stream/movie/tt0133093.json"   # noqa: E731
        stavy = []
        for _ in range(3):
            k = config.encode(config.from_mapping({**NASTAVENI, "id": r.identita.vydat()}))
            stavy.append(r.route(cesta(k), ZAKLAD, klient="1.2.3.4").status)
        self.assertEqual(stavy, [200, 200, 200])
        k = config.encode(config.from_mapping({**NASTAVENI, "id": r.identita.vydat()}))
        self.assertEqual(r.route(cesta(k), ZAKLAD, klient="1.2.3.4").status, 429, "4. identita, strop adresy")
        self.assertEqual(r.route(cesta(k), ZAKLAD, klient="5.6.7.8").status, 200, "jiná adresa jede")

    def test_play_ma_limit_a_blokaci(self):
        """Audit 2026-09-19: `/play/` neměl limit — sto tisíc rozklíčování = HellSpy 429 pro všechny."""
        from nokturno import routes
        r = router()
        r.play_okno = routes.Okno(2, 600)
        r.blokace = routes.Blokace(prah=2, okno_s=600, doba_s=3600)
        cesta = f"/c/{KOUSEK}/play/{mapping.zakoduj('ws:abc')}"
        stavy = [r.route(cesta, ZAKLAD, klient="1.2.3.4").status for _ in range(5)]
        self.assertEqual(stavy[:2], [302, 302])
        self.assertEqual(stavy[2], 429)
        self.assertEqual(stavy[-1], 403, "po prahu odmítnutí blokace i na /play/")
        self.assertEqual(r.route(f"/c/{KOUSEK}/manifest.json", ZAKLAD, klient="1.2.3.4").status, 200)

    def test_katalog_ma_limit_a_strop_skip(self):
        from nokturno import routes
        from nokturno.katalogy import Katalogy
        volani = []

        class Sosac:
            def catalog(self, ctype, cid, skip=0, page=100):
                volani.append(skip)
                return [{"id": "sosacd_m_x", "imdb_id": "tt0133093", "name": "Matrix", "year": "1999"}]
        r = router()
        r.katalogy = Katalogy(tempfile.mkdtemp())
        r.katalogy.sosac = Sosac()
        r.katalog_okno = routes.Okno(1, 600)
        cesta = "/catalog/movie/nokturno.sosac.nove.dabing.json"
        self.assertEqual(r.route(f"/c/{KOUSEK}{cesta}", ZAKLAD, klient="1.2.3.4").status, 200)
        self.assertEqual(r.route(f"/c/{KOUSEK}{cesta}", ZAKLAD, klient="1.2.3.4").status, 429)
        self.assertEqual(r.route(f"/c/{KOUSEK}{cesta}", ZAKLAD, klient="5.6.7.8").status, 200, "jiná adresa jede")
        r.katalog_okno = routes.Okno(100, 600)
        odp = r.route(f"/c/{KOUSEK}/catalog/movie/nokturno.sosac.nove.dabing/skip={routes.MAX_SKIP + 1}.json",
                      ZAKLAD, klient="1.2.3.4")
        self.assertEqual(odp.status, 200)
        self.assertEqual(odp.data, {"metas": []}, "za stropem prázdno bez dotazu na zdroj")
        self.assertNotIn(routes.MAX_SKIP + 1, volani)

    def test_check_limit_po_prefixu_ipv6(self):
        from nokturno.routes import Okno
        r = router()
        r.ws_api = FalesnyWebshare
        r.check_okno = Okno(1, 300)
        kousek = config.encode(config.from_mapping({"ws_username": "u", "ws_password": "spravne"}))
        self.assertEqual(r.route(f"/c/{kousek}/check", ZAKLAD, klient="2001:db8::1").status, 200)
        self.assertEqual(r.route(f"/c/{kousek}/check", ZAKLAD, klient="2001:db8::2").status, 429,
                         "jiná adresa v témž /64 sdílí limit")

    def test_okno_pri_preteceni_nemaze_vse(self):
        """Audit 2026-09-19: 5001 klíčů dřív smazalo celý slovník včetně blokací."""
        from nokturno import routes
        o = routes.Okno(1, 600, max_keys=3)
        for k in ("a", "b", "c"):
            self.assertTrue(o.povolit(k))
        self.assertFalse(o.povolit("a"), "limit platí dál")
        self.assertFalse(o.povolit("d"), "plno a nic neprošlo → nový klíč se odmítne, staré zůstávají")
        o._data["b"] = (1, time.time() - 601)   # jedno okno prošlo
        self.assertTrue(o.povolit("d"))
        self.assertFalse(o.povolit("a"))
        b = routes.Blokace(prah=1, okno_s=600, doba_s=3600, max_klicu=2)
        b.prohresek("x")
        b.prohresek("y")
        self.assertTrue(b.blokovana("x"))
        b.prohresek("z")
        self.assertTrue(b.blokovana("x"), "přetečení nesmí odblokovat staré")
        self.assertTrue(b.blokovana("y"))

    def test_cors_jen_na_protokol(self):
        from nokturno.server import cors_povoleno
        for c in ("/health", "/manifest.json", f"/c/{KOUSEK}/manifest.json", f"/c/{KOUSEK}/stream/movie/tt1.json",
                  f"/c/{KOUSEK}/catalog/movie/x/skip=20.json", f"/c/{KOUSEK}/play/abc", "/catalog/movie/x.json"):
            self.assertTrue(cors_povoleno(c), c)
        for c in ("/", "/configure", f"/c/{KOUSEK}/configure", f"/c/{KOUSEK}/check", "/identita/vyzva",
                  "/identita?vyzva=1&reseni=2", "/configure?lang=sk"):
            self.assertFalse(cors_povoleno(c), c)

    def test_druha_blokace_identity_ji_odebere_natrvalo(self):
        from nokturno import routes
        from nokturno.identita import Identita
        with tempfile.TemporaryDirectory() as tmp:
            soubor = os.path.join(tmp, "odebrane.txt")
            r = router()
            r.identita = Identita("tajne")
            r.stream_okno = routes.Okno(1, 600)
            r.blokace = routes.Blokace(prah=2, okno_s=600, doba_s=0, soubor=soubor)
            t = r.identita.vydat()
            k = config.encode(config.from_mapping({**NASTAVENI, "id": t}))
            cesta = f"/c/{k}/stream/movie/tt0133093.json"
            stavy = [r.route(cesta, ZAKLAD, klient="1.2.3.4").status for _ in range(9)]
            # 200, 429, 429(→1. blokace, doba 0 → hned vyprší), 429, 429(→2. blokace = odebrání), pak 403 napořád
            self.assertEqual(stavy[-1], 403)
            self.assertTrue(r.blokace.odebrana("id:" + t))
            self.assertEqual(r.route(cesta, ZAKLAD, klient="1.2.3.4").utok[0], "odebráno")
            # formulář odebranou zahodí (vydá se nová), soubor přežije restart
            html = r.route(f"/c/{k}/configure", ZAKLAD, klient="1.2.3.4").html
            self.assertIn('form.elements["id"].value = ""', html)
            self.assertTrue(routes.Blokace(soubor=soubor).odebrana("id:" + t))

    def test_klic_klienta(self):
        from nokturno.routes import klic_klienta
        self.assertEqual(klic_klienta("1.2.3.4"), "1.2.3.4")
        self.assertEqual(klic_klienta("::ffff:1.2.3.4"), "1.2.3.4")
        self.assertEqual(klic_klienta("2a09:bac1:1da0:10::1f:b9"), "2a09:bac1:1da0:10::/64")
        self.assertEqual(klic_klienta(""), "")

    def test_limit_streamu_bez_ip_podle_otisku(self):
        from nokturno import routes
        r = router()
        r.stream_okno = routes.Okno(3, 600)
        cesta = f"/c/{KOUSEK}/stream/movie/tt0133093.json"
        self.assertEqual([r.route(cesta, ZAKLAD).status for _ in range(4)], [200, 200, 200, 429])

    def test_jina_adresa_blokaci_neni_dotcena(self):
        r = Router(FalesneEnginy(FalesnyEngine()), blokovane={"jiny-otisk"})
        odp = r.route(f"/c/{KOUSEK}/manifest.json", ZAKLAD)
        self.assertEqual(odp.status, 200)

    def test_uklid_starych_slozek_jader(self):
        import os
        import time
        from nokturno.server import uklid_dat
        tmp = pathlib.Path(tempfile.mkdtemp())
        stara, nova, cizi = tmp / ("a" * 16), tmp / ("b" * 16), tmp / "neco-jineho"
        for d in (stara, nova, cizi):
            d.mkdir()
        os.utime(stara, (time.time() - 40 * 86400,) * 2)
        os.utime(cizi, (time.time() - 40 * 86400,) * 2)
        self.assertEqual(uklid_dat(str(tmp)), 1)
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), sorted([nova.name, cizi.name]))

    def test_uklid_cache_smaze_jen_prosle(self):
        import os
        import time
        from nokturno.server import uklid_cache
        tmp = pathlib.Path(tempfile.mkdtemp())
        cdir = tmp / ("a" * 16) / "cache"
        cdir.mkdir(parents=True)
        stary, novy = cdir / "stary.json", cdir / "novy.json"
        stary.write_text("{}")
        novy.write_text("{}")
        os.utime(stary, (time.time() - 100 * 3600,) * 2)
        self.assertEqual(uklid_cache(str(tmp), max_age_s=72 * 3600), 1)
        self.assertFalse(stary.exists())
        self.assertTrue(novy.exists())

    def test_uklid_cache_bez_cache_slozky_nic(self):
        from nokturno.server import uklid_cache
        tmp = pathlib.Path(tempfile.mkdtemp())
        (tmp / ("a" * 16)).mkdir()
        self.assertEqual(uklid_cache(str(tmp)), 0)

    def test_statistiky_nedrzi_neomezene(self):
        from nokturno.statistiky import Statistiky
        st = Statistiky("v")
        st.limit = 5
        for i in range(12):
            class E:
                class store:
                    dir = tempfile.mkdtemp()
            st._pro(E())
        self.assertEqual(len(st._stats), 5)


class TestStylFormulare(unittest.TestCase):
    def test_kazdy_typ_inputu_ma_styl(self):
        """Pole adresy úložiště (`type=url`) bylo bez stylu — selektor vyjmenovával jen text,
        password a number (2026-09-14)."""
        import re
        for jmeno in ("configure.html", "configure.sk.html"):
            html = (ROOT / "nokturno" / "static" / jmeno).read_text(encoding="utf-8")
            # hidden (volba katalogů) se nezobrazuje, styl nepotřebuje
            typy = set(re.findall(r'<input type="([a-z]+)"', html)) - {"button", "checkbox", "submit", "hidden"}
            selektor = re.search(r"^\s*(input\[type=[^{]+)\{", html, re.M).group(1)
            stylovane = set(re.findall(r"input\[type=([a-z]+)\]", selektor))
            self.assertEqual(typy - stylovane, set(), f"{jmeno}: input bez stylu")


class TestHeadAProxyKodovani(unittest.TestCase):
    def test_head_na_streamy_nespousti_hledani(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from nokturno.routes import Odpoved
        from nokturno.server import Handler
        volani = []

        class Smerovac:
            def route(self, cesta, zaklad, verejny=False, jazyk=None, klient="", aplikace="stremio"):
                volani.append(cesta)
                return Odpoved(data={"ok": True})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.router = Smerovac()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        zaklad = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            for cesta in ("/c/abc/stream/movie/tt1.json", "/c/abc/check"):
                with urllib.request.urlopen(urllib.request.Request(zaklad + cesta, method="HEAD"), timeout=5) as resp:
                    self.assertEqual(resp.status, 204, cesta)
            self.assertEqual(volani, [], "HEAD na streamy/check se k routeru nedostane")
            with urllib.request.urlopen(urllib.request.Request(zaklad + "/health", method="HEAD"), timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            self.assertEqual(volani, ["/health"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_env_example_a_compose_znaji_vsechny_promenne(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for name in ("NOKTURNO_ST_EMAIL", "NOKTURNO_DAV1_URL", "NOKTURNO_STATS", "NOKTURNO_HOST", "NOKTURNO_CONFIGURE_PREFILL"):
            self.assertIn(name, env, name)
        for name in ("NOKTURNO_ST_EMAIL", "NOKTURNO_DAV1_URL", "NOKTURNO_STATS"):
            self.assertIn(name, compose, name)
        self.assertNotIn("NOKTURNO_TMDB_API_KEY", compose, "Stremio TMDB nečte")


class TestHlaseniOPadech(unittest.TestCase):
    """Neošetřená výjimka v `Handler.do_GET` → 500 klientovi a hlášení ve frontě `<data>/pady/`."""

    def test_akce_z_cesty_bez_nastaveni_a_id(self):
        from nokturno.pady import akce_z_cesty
        self.assertEqual(akce_z_cesty("/c/eyJ3cyI6MX0/stream/movie/tt1.json"), "stream/movie")
        self.assertEqual(akce_z_cesty("/manifest.json?x=1"), "manifest.json")
        self.assertEqual(akce_z_cesty("/"), "/")

    def test_pad_pri_pozadavku(self):
        import http.client
        import json
        import threading
        from unittest import mock
        from nokturno import server as srv

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(srv.Pady, "odesli") as odesli, \
                mock.patch.dict("os.environ", {"NOKTURNO_CRASH_REPORTS": "1"}):
            httpd, _ = srv.vytvor_server("127.0.0.1", 0, tmp, options={})
            httpd.router.route = mock.Mock(side_effect=ZeroDivisionError("token=tajne"))
            vlakno = threading.Thread(target=httpd.serve_forever, daemon=True)
            vlakno.start()
            try:
                spojeni = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
                spojeni.request("GET", "/c/eyJ3cyI6InVzZXIifQ/stream/movie/tt1.json")
                self.assertEqual(spojeni.getresponse().status, 500)
            finally:
                httpd.shutdown()
                httpd.server_close()
            [soubor] = httpd.pady.reporter.pending()
            with open(soubor, encoding="utf-8") as f:
                hlaseni = json.load(f)
            self.assertEqual((hlaseni["product"], hlaseni["action"], hlaseni["type"]),
                             ("stremio", "stream/movie", "ZeroDivisionError"))
            self.assertNotIn("tajne", hlaseni["message"] + hlaseni["traceback"])
            self.assertRegex(hlaseni["id"], r"^[0-9a-f]{32}$")
            self.assertGreaterEqual(odesli.call_count, 2)   # po startu + po pádu

    def test_vypnuto_promennou(self):
        from nokturno.pady import Pady
        with tempfile.TemporaryDirectory() as tmp:
            pady = Pady.z_prostredi(tmp, "5.2.11", environ={"NOKTURNO_CRASH_REPORTS": "0"})
            self.assertFalse(pady.zaznamenej(RuntimeError("x"), "/"))
            self.assertEqual(pady.reporter.pending(), [])


class TestProvoz(unittest.TestCase):
    """Hlášení provozu do dashboardu (`nokturno/provoz.py`, obrazovka Provoz)."""

    def test_klasifikace_cest(self):
        from nokturno.provoz import klasifikuj
        self.assertEqual(klasifikuj("/c/eyJ3cyI6MX0/manifest.json"),
                         ("stremio", "/c/{nastaveni}/manifest.json"))
        self.assertEqual(klasifikuj("/c/eyJ3cyI6MX0/stream/movie/tt1.json"),
                         ("stremio", "/c/{nastaveni}/stream/movie"))
        self.assertEqual(klasifikuj("/c/eyJ3cyI6MX0/play/dlouhy-podpis?x=1"),
                         ("prehravani", "/c/{nastaveni}/play"))
        self.assertEqual(klasifikuj("/catalog/series/sosac.nove.serialy.dabing.json"),
                         ("stremio", "/catalog/series"))
        self.assertEqual(klasifikuj("/"), ("stremio", "/"))
        self.assertEqual(klasifikuj("/health"), ("stremio", "/health"))


    def test_ucty_z_adresy_se_nikam_neposlou(self):
        from nokturno.provoz import Provoz
        p = Provoz(token="t")
        p.zaznamenej("/c/eyJ3cyI6InVzZXIiLCJwYXNzIjoidGFqbmUifQ/play/x", "GET", 206, 10 ** 7, 50)
        [radek] = p._fronta
        self.assertNotIn("eyJ3cyI6", radek["route"])
        self.assertEqual(radek["service"], "prehravani")
        self.assertEqual(radek["bytes_out"], 10 ** 7)

    def test_utoky_se_slucuji_a_nejdou_do_provozu(self):
        from unittest import mock
        from nokturno.provoz import Provoz
        p = Provoz(token="t")
        for _ in range(3):
            p.zaznamenej_utok("2001:db8::1", "Python/3.12 aiohttp", "abcdef0123456789xyz", "limit")
        p.zaznamenej_utok("2001:db8::2", None, None, "blokováno")
        self.assertEqual(p._fronta, [])
        poslano = []
        with mock.patch.object(Provoz, "_posli_davku", lambda self, d, u=None: poslano.append((d, u)) or True):
            p.odesli()
        [(davka, utoky)] = poslano
        self.assertEqual(davka, [])
        prvni = next(u for u in utoky if u["ip"] == "2001:db8::1")
        self.assertEqual((prvni["hits"], prvni["reason"], prvni["fp"]), (3, "limit", "abcdef0123456789"))
        self.assertEqual(p._utoky, {})

    def test_utok_nese_priznak_identity(self):
        from unittest import mock
        from nokturno.provoz import Provoz
        p = Provoz(token="t")
        p.zaznamenej_utok("a", "x", "fp", "limit", ma_id=True)
        p.zaznamenej_utok("b", "x", "fp", "limit", ma_id=False)
        p.zaznamenej_utok("c", "x", "fp", "limit")
        poslano = []
        with mock.patch.object(Provoz, "_posli_davku", lambda self, d, u=None: poslano.append(u) or True):
            p.odesli()
        self.assertEqual({u["ip"]: u["has_id"] for u in poslano[0]}, {"a": 1, "b": 0, "c": -1})

    def test_upozorneni_na_novou_adresu(self):
        from nokturno import mapping
        nova = "https://x.example/configure"
        self.assertIn("Nastavení", mapping.manifest("6.4.6", (), nova_adresa=nova)["description"])
        self.assertNotIn("⚠️", mapping.manifest("6.4.6", ())["description"])
        u = mapping.upozorneni_nova_adresa(nova)
        self.assertEqual(u["externalUrl"], nova)
        self.assertTrue(u["name"].startswith("⚠️"))

    def test_stara_adresa_dostane_jen_vyzvu(self):
        from nokturno.identita import Identita
        r = router()
        r.identita = Identita("tajne")
        st = r.route(f"/c/{KOUSEK_HS}/stream/movie/tt0133093.json", ZAKLAD)
        self.assertEqual(len(st.data["streams"]), 1)
        self.assertIn("Nastavení", st.data["streams"][0]["title"])
        self.assertEqual(r.route(f"/c/{KOUSEK_HS}/play/abc", ZAKLAD).status, 410)
        # s vlastními účty se stará adresa nechává
        self.assertNotIn("Nastavení", str(r.route(f"/c/{KOUSEK}/stream/movie/tt0133093.json", ZAKLAD).data))

    def test_provoz_rozlisuje_adresu_s_identitou(self):
        from nokturno.provoz import klasifikuj
        s_id = config.encode({**NASTAVENI, config.ID_KLIC: "0123456789abcdef.0123456789abcdef"})
        self.assertEqual(klasifikuj(f"/c/{s_id}/stream/movie/tt1.json")[1], "/c/{nastaveni s identitou}/stream/movie")
        self.assertEqual(klasifikuj(f"/c/{KOUSEK_HS}/stream/movie/tt1.json")[1], "/c/{nastaveni}/stream/movie")
        self.assertEqual(klasifikuj(f"/c/{KOUSEK}/stream/movie/tt1.json")[1], "/c/{nastaveni s účty}/stream/movie")

    def test_ma_identitu_z_cesty(self):
        r = router()
        s_id = config.encode({**NASTAVENI, config.ID_KLIC: "0123456789abcdef.0123456789abcdef"})
        s_ucty = config.encode({"hs_enabled": True, "ws_username": "u", "ws_password": "p"})
        s_hs = config.encode({"hs_enabled": True})
        self.assertEqual(r.ma_identitu(f"/c/{s_id}/stream/movie/tt1.json"), 1)
        self.assertEqual(r.ma_identitu(f"/c/{s_ucty}/stream/movie/tt1.json"), 2)
        self.assertEqual(r.ma_identitu(f"/c/{s_hs}/stream/movie/tt1.json"), 0)
        self.assertEqual(r.ma_identitu("/catalog/movie/x.json"), 0)

    def test_limit_na_otisk_uctu_ne_na_ip(self):
        """Dva lidé s vlastními účty za jednou IP si limit nedělí; nastavení jen s HellSpy ano."""
        r = router()
        a = config.encode({"hs_enabled": True, "ws_username": "a", "ws_password": "1"})
        b = config.encode({"hs_enabled": True, "ws_username": "b", "ws_password": "2"})
        h = config.encode({"hs_enabled": True})
        ka = r._klic_limitu(config.decode(a), "1.2.3.4")
        kb = r._klic_limitu(config.decode(b), "1.2.3.4")
        self.assertNotEqual(ka, kb)
        self.assertTrue(ka.startswith("fp:"))
        self.assertEqual(r._klic_limitu(config.decode(h), "1.2.3.4"), "1.2.3.4")

    def test_utoky_maji_strop(self):
        from nokturno import provoz
        p = provoz.Provoz(token="t")
        for i in range(provoz.STROP_UTOKU + 50):
            p.zaznamenej_utok(f"10.0.{i // 250}.{i % 250}", "x", "fp", "limit")
        self.assertEqual(len(p._utoky), provoz.STROP_UTOKU)

    def test_bez_tokenu_je_vypnuto(self):
        from nokturno.provoz import Provoz
        p = Provoz.z_prostredi(environ={})
        self.assertFalse(p.zapnuto)
        p.zaznamenej("/manifest.json", "GET", 200, 10)
        self.assertEqual(p._fronta, [])
        vypnuto = Provoz.z_prostredi(environ={"NOKTURNO_TRAFFIC_TOKEN": "t", "NOKTURNO_TRAFFIC": "0"})
        self.assertFalse(vypnuto.zapnuto)

    def test_fronta_ma_strop_a_odeslani_zahodi_pri_chybe(self):
        from unittest import mock
        from nokturno.provoz import Provoz
        p = Provoz(token="t", strop=2)
        for _ in range(5):
            p.zaznamenej("/manifest.json", "GET", 200, 1)
        self.assertEqual(len(p._fronta), 2)
        self.assertEqual(p.zahozeno, 3)
        with mock.patch.object(Provoz, "_posli_davku", return_value=False):
            self.assertEqual(p.odesli(), 0)
        self.assertEqual(p._fronta, [])          # fronta neroste, když dashboard neodpovídá

    def test_pozadavek_se_zmeri_vcetne_bajtu(self):
        import http.client
        import threading
        from unittest import mock
        from nokturno import server as srv

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict("os.environ", {"NOKTURNO_TRAFFIC_TOKEN": "t"}), \
                mock.patch.object(srv.Provoz, "start"):        # bez odesílacího vlákna
            httpd, _ = srv.vytvor_server("127.0.0.1", 0, tmp, options={})
            vlakno = threading.Thread(target=httpd.serve_forever, daemon=True)
            vlakno.start()
            try:
                spojeni = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
                spojeni.request("GET", "/health")
                self.assertEqual(spojeni.getresponse().read() and 200, 200)
                spojeni.request("GET", "/health")              # druhý na témž spojení (keep-alive)
                spojeni.getresponse().read()
            finally:
                httpd.shutdown()
                httpd.server_close()
            radky = httpd.provoz._fronta
            self.assertEqual(len(radky), 2, "měření se resetuje u každého požadavku, ne u spojení")
            for radek in radky:
                self.assertEqual((radek["service"], radek["route"], radek["status"]),
                                 ("stremio", "/health", 200))
                self.assertGreater(radek["bytes_out"], 0)


class TestStropSoubeznychSpojeni(unittest.TestCase):
    """Nález 26 z auditu: `ThreadingHTTPServer` zakládá vlákno na každé otevřené
    spojení a slowloris jich udrží, kolik stihne otevřít."""

    def _server(self, max_spojeni, tmp):
        import threading
        from nokturno import server as srv
        httpd, _ = srv.vytvor_server("127.0.0.1", 0, tmp, options={})
        httpd.max_spojeni = max_spojeni
        httpd._volno = threading.BoundedSemaphore(max_spojeni)
        # test zavírá spojení, na kterém server čeká na další požadavek; výsledný
        # ConnectionResetError je očekávaný a nemá zaplavovat výstup testů
        httpd.handle_error = lambda *_: None
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def _pockej_na_volno(self, httpd):
        for _ in range(100):
            if httpd._volno.acquire(blocking=False):
                httpd._volno.release()
                return True
            time.sleep(0.05)
        return False

    def test_nad_strop_prijde_503_a_spojeni_se_zavre(self):
        import http.client

        with tempfile.TemporaryDirectory() as tmp:
            httpd = self._server(1, tmp)
            port = httpd.server_address[1]
            drzi = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                # první spojení zůstane otevřené a drží jediné povolené místo
                drzi.request("GET", "/health")
                self.assertEqual(drzi.getresponse().read() and 200, 200)

                druhe = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                druhe.request("GET", "/health")
                odpoved = druhe.getresponse()
                self.assertEqual(odpoved.status, 503)
                self.assertEqual(odpoved.getheader("Retry-After"), "5")
                self.assertEqual(odpoved.read(), b"")
                self.assertEqual(httpd.odmitnuta_spojeni, 1)
                druhe.close()

                # po uvolnění prvního spojení server zase obsluhuje
                drzi.close()
                self.assertTrue(self._pockej_na_volno(httpd), "místo se nevrátilo")
                dalsi = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                dalsi.request("GET", "/health")
                self.assertEqual(dalsi.getresponse().status, 200)
                dalsi.close()
                self.assertEqual(httpd.odmitnuta_spojeni, 1, "odmítá se jen nad stropem")
            finally:
                drzi.close()
                httpd.shutdown()
                httpd.server_close()

    def test_odmitnute_spojeni_nezalozi_vlakno_ani_radek_provozu(self):
        import http.client
        import threading
        from unittest import mock
        from nokturno import server as srv

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict("os.environ", {"NOKTURNO_TRAFFIC_TOKEN": "t"}), \
                mock.patch.object(srv.Provoz, "start"):        # bez odesílacího vlákna
            httpd = self._server(1, tmp)
            port = httpd.server_address[1]
            drzi = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                drzi.request("GET", "/health")
                drzi.getresponse().read()
                pred = threading.active_count()

                for _ in range(5):
                    odmitnute = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    odmitnute.request("GET", "/health")
                    self.assertEqual(odmitnute.getresponse().status, 503)
                    odmitnute.close()

                self.assertLessEqual(threading.active_count(), pred,
                                     "odmítnuté spojení nesmí založit vlákno")
                self.assertEqual(httpd.odmitnuta_spojeni, 5)
                self.assertEqual([r["route"] for r in httpd.provoz._fronta], ["/health"],
                                 "odmítnuté spojení se do provozu nepočítá")
            finally:
                drzi.close()
                httpd.shutdown()
                httpd.server_close()

    def test_misto_se_vrati_i_kdyz_obsluha_spadne(self):
        import threading
        from unittest import mock
        from nokturno import server as srv

        with tempfile.TemporaryDirectory() as tmp:
            httpd, _ = srv.vytvor_server("127.0.0.1", 0, tmp, options={})
            try:
                httpd.max_spojeni = 2
                httpd._volno = threading.BoundedSemaphore(2)
                with mock.patch.object(srv.ThreadingHTTPServer, "process_request_thread",
                                       side_effect=RuntimeError("bum")):
                    for _ in range(5):
                        httpd._volno.acquire()
                        with self.assertRaises(RuntimeError):
                            httpd.process_request_thread(mock.Mock(), ("127.0.0.1", 1))
                # obě místa jsou zpátky volná, i když každá obsluha spadla
                self.assertTrue(httpd._volno.acquire(blocking=False))
                self.assertTrue(httpd._volno.acquire(blocking=False))
                self.assertFalse(httpd._volno.acquire(blocking=False))
            finally:
                httpd.server_close()

    def test_misto_se_vrati_kdyz_vlakno_nejde_zalozit(self):
        import threading
        from unittest import mock
        from nokturno import server as srv

        with tempfile.TemporaryDirectory() as tmp:
            httpd, _ = srv.vytvor_server("127.0.0.1", 0, tmp, options={})
            try:
                httpd.max_spojeni = 1
                httpd._volno = threading.BoundedSemaphore(1)
                with mock.patch.object(srv.ThreadingHTTPServer, "process_request",
                                       side_effect=RuntimeError("can't start new thread")):
                    with self.assertRaises(RuntimeError):
                        httpd.process_request(mock.Mock(), ("127.0.0.1", 1))
                self.assertTrue(httpd._volno.acquire(blocking=False),
                                "po selhání `Thread.start` musí místo zůstat volné")
            finally:
                httpd.server_close()

    def test_vychozi_strop_fronta_a_timeout_spojeni(self):
        from nokturno import server as srv
        self.assertEqual(srv.MAX_SPOJENI, 400)
        self.assertEqual(srv.Handler.timeout, 30)
        self.assertEqual(srv.Server.request_queue_size, 128,
                         "výchozích 5 ze socketserveru je tvrdší strop než MAX_SPOJENI")
        self.assertTrue(srv.Server.daemon_threads)

    def test_odmitnuty_pozadavek_zavre_spojeni(self):
        """Odpověď s `utok` (limit, blokace) nesmí nechat spojení viset v keep-alive —
        držela by vlákno `Handler.timeout` sekund za odpověď, která trvá milisekundu."""
        import http.client
        from unittest import mock
        from nokturno import server as srv
        from nokturno.routes import Odpoved

        with tempfile.TemporaryDirectory() as tmp:
            httpd = self._server(50, tmp)
            port = httpd.server_address[1]
            try:
                with mock.patch.object(httpd.router, "route",
                                       return_value=Odpoved(429, text="moc dotazů",
                                                            utok=("limit", "abcdef0123456789"))):
                    spojeni = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    spojeni.request("GET", "/health")
                    odpoved = spojeni.getresponse()
                    self.assertEqual(odpoved.status, 429)
                    self.assertEqual(odpoved.getheader("Connection"), "close")
                    odpoved.read()
                    spojeni.close()

                # a naopak: běžná odpověď keep-alive drží (dva dotazy na jednom spojení)
                spojeni = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                spojeni.request("GET", "/health")
                prvni = spojeni.getresponse()
                self.assertNotEqual(prvni.getheader("Connection"), "close")
                prvni.read()
                spojeni.request("GET", "/health")
                self.assertEqual(spojeni.getresponse().status, 200)
                spojeni.close()
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_misto_se_vrati_i_po_odmitnutem_pozadavku(self):
        """Uzavření spojení po odmítnutí nesmí místo v semaforu ztratit."""
        import http.client
        from unittest import mock
        from nokturno import server as srv
        from nokturno.routes import Odpoved

        with tempfile.TemporaryDirectory() as tmp:
            httpd = self._server(2, tmp)
            port = httpd.server_address[1]
            try:
                with mock.patch.object(httpd.router, "route",
                                       return_value=Odpoved(403, text="blokováno",
                                                            utok=("blokováno", None))):
                    for _ in range(6):
                        spojeni = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                        spojeni.request("GET", "/health")
                        self.assertEqual(spojeni.getresponse().status, 403)
                        spojeni.close()
                        self.assertTrue(self._pockej_na_volno(httpd))
                self.assertEqual(httpd.odmitnuta_spojeni, 0,
                                 "šest odmítnutých požadavků za sebou nesmí vyčerpat dvě místa")
            finally:
                httpd.shutdown()
                httpd.server_close()
