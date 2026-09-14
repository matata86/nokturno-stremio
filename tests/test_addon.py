"""Kontrola vrstvy nad jádrem — bez sítě a bez účtů.

    python3 -m unittest discover -s tests -v

Jádro má vlastní testy v repu `nokturno-core`. Tady se ověřuje jen to, co je
vlastní doplňku: převod streamů do podoby pro Stremio, rozcestník a odmítání
odkazů, které by se neměly přehrát.
"""
import logging
import pathlib
import sys
import tempfile
import unittest
import urllib.error

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

    def pro(self, options=None, verejny=False):
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
        chybi = [k for k in config.PROSTREDI.values() if f'"{k}"' not in zdroj and k != "hs_enabled"]
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

    def test_vypnuti_promennou(self):
        from nokturno.statistiky import Statistiky
        self.assertFalse(Statistiky.z_prostredi("1", {"NOKTURNO_STATS": "0"}).zapnuto)
        self.assertTrue(Statistiky.z_prostredi("1", {}).zapnuto)

    def test_router_zaznamena_zobrazene_streamy(self):
        r = router()
        volani = []
        r.statistiky = type("S", (), {"zaznamenej": lambda self, *a: volani.append(a)})()
        r.route(f"/c/{KOUSEK}/stream/movie/tt1.json", ZAKLAD)
        self.assertEqual(volani[0][1:], ("movie", "tt1"))



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
    """Úložiště s heslem: odkaz jde přes proxy doplňku, z internetu ne na localhost."""

    def test_odkaz_projde_dekodovanim(self):
        self.assertEqual(mapping.dekoduj(mapping.zakoduj("dav:1:Filmy/a b.mkv")), "dav:1:Filmy/a b.mkv")

    def test_prehrani_jde_pres_proxy(self):
        r = router()
        r.engine.storage_request = lambda url: ("http://nas.lan/dav/Filmy/a.mkv", {"Authorization": "Basic x"})
        odpoved = r.route("/play/" + mapping.zakoduj("dav:1:Filmy/a.mkv"), ZAKLAD)
        self.assertEqual(odpoved.proxy, ("http://nas.lan/dav/Filmy/a.mkv", {"Authorization": "Basic x"}))
        self.assertIsNone(odpoved.location)

    def test_neznamy_slot_je_404(self):
        r = router()

        def spatne(url):
            raise NokturnoError("Tohle úložiště už není v nastavení.")
        r.engine.storage_request = spatne
        self.assertEqual(r.route("/play/" + mapping.zakoduj("dav:3:a.mkv"), ZAKLAD).status, 404)

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

    def test_proxy_preposle_range_a_heslo(self):
        import threading
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from nokturno.routes import Odpoved
        from nokturno.server import Handler

        videno = {}

        class Zdroj(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                videno["auth"] = self.headers.get("Authorization")
                videno["range"] = self.headers.get("Range")
                if self.headers.get("Authorization") != "Basic ok":
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", "bytes 2-5/10")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"2345")

        zdroj = ThreadingHTTPServer(("127.0.0.1", 0), Zdroj)
        doplnek = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        url = f"http://127.0.0.1:{zdroj.server_address[1]}/a.mkv"

        class Smerovac:
            heslo = "Basic ok"

            def route(self, cesta, zaklad, verejny=False, jazyk=None, klient=""):
                return Odpoved(proxy=(url, {"Authorization": self.heslo}))
        doplnek.router = Smerovac()
        for s in (zdroj, doplnek):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        adresa = f"http://127.0.0.1:{doplnek.server_address[1]}/play/x"
        # domácí požadavek = z tailnetu přes `tailscale serve`; bez té hlavičky je
        # požadavek z 127.0.0.1 při pochybnosti veřejný (viz je_verejny)
        doma = {"Tailscale-User-Login": "ja@tailnet"}
        try:
            req = urllib.request.Request(adresa, headers={"Range": "bytes=2-5", **doma})
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual((resp.status, resp.read(), resp.headers["Content-Range"]),
                                 (206, b"2345", "bytes 2-5/10"))
            self.assertEqual(videno, {"auth": "Basic ok", "range": "bytes=2-5"})
            Smerovac.heslo = "Basic spatne"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(urllib.request.Request(adresa, headers=doma), timeout=5)
            self.assertEqual(ctx.exception.code, 502)
            ctx.exception.close()
            # z internetu na zdroj v naší síti proxy nesmí — ani se správným heslem
            Smerovac.heslo = "Basic ok"
            videno.clear()
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(urllib.request.Request(adresa, headers={"Tailscale-Funnel-Request": "?1"}),
                                       timeout=5)
            self.assertEqual(ctx.exception.code, 502)
            ctx.exception.close()
            self.assertEqual(videno, {}, "zdroj se z internetu nesmí ani oslovit")
        finally:
            for s in (zdroj, doplnek):
                s.shutdown()
                s.server_close()


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
            def route(self, cesta, zaklad, verejny=False, jazyk=None, klient=""):
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
