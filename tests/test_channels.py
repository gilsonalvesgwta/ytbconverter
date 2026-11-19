"""
Testes da aba "Achar canais" contra uma API do YouTube simulada (sem internet, sem cota).
Uso: python3 tests/test_channels.py
"""

import datetime as dt
import http.server
import json
import os
import sys
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_e2e as base  # noqa: E402

PASSWORD = "senha-canais-1"
AUTH = {"X-Access-Key": PASSWORD}
KEY = "AIzaSyTESTE_chave_valida_0123456789abcd"
NOW = dt.datetime.now(dt.timezone.utc)


def iso(days_ago):
    return (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def cid(ch):
    return "UC" + (ch * 22)[:22]


CHANNELS = {
    # canal de playlists novo e explodindo
    "A": {"created": 5, "subs": "2500", "views": "3000000", "videos": "6", "country": "US", "title": "Nova Slow Rock"},
    # canal novo, perto de monetizar
    "B": {"created": 20, "subs": "700", "views": "300000", "videos": "12", "country": "BR", "title": "Louvores de Paz"},
    # canal antigo (precisa ser descartado)
    "C": {"created": 800, "subs": "900000", "views": "90000000", "videos": "400", "country": "GB", "title": "Old Hits"},
    # canal novo com vídeo de poucas views (descartado pelo mínimo) e inscritos ocultos
    "D": {"created": 3, "subs": None, "views": "50000", "videos": "2", "country": "", "title": "Tiny Beats"},
    # canal de Shorts novo
    "E": {"created": 10, "subs": "5000", "views": "12000000", "videos": "30", "country": "IN", "title": "Shorts Music"},
}
VIDEOS = {  # id(11) -> canal, views, duração, dias
    "vidA1xxxxxx": ("A", 1_800_000, "PT1H2M10S", 4), "vidA2xxxxxx": ("A", 900_000, "PT58M", 2),
    "vidB1xxxxxx": ("B", 150_000, "PT3M", 15),
    "vidC1xxxxxx": ("C", 5_000_000, "PT4M", 10),
    "vidD1xxxxxx": ("D", 40_000, "PT2M", 2),
    "vidE1xxxxxx": ("E", 3_000_000, "PT30S", 6),
}


class FakeYT(http.server.BaseHTTPRequestHandler):
    log = []

    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json"):
        body = (json.dumps(obj) if not isinstance(obj, str) else obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/watch":
            vid = q.get("v")
            if vid == "vidA1xxxxxx":
                return self.send(200, '<script>var ytInitialPlayerResponse = {"adPlacements": [{"adPlacementRenderer":{}}]};</script>', "text/html")
            if vid == "vidB1xxxxxx":
                return self.send(200, '<script>var ytInitialPlayerResponse = {"playabilityStatus":{}};</script>', "text/html")
            return self.send(200, "<html>consent.youtube.com</html>", "text/html")
        FakeYT.log.append((u.path, q))
        key = q.get("key", "")
        if key.startswith("AIzaSyBAD"):
            return self.send(400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                             "errors": [{"reason": "badRequest"}],
                                             "details": [{"reason": "API_KEY_INVALID"}]}})
        if key.startswith("AIzaSyQUOTA"):
            return self.send(403, {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
        if key.startswith("AIzaSyOFF"):
            return self.send(403, {"error": {"code": 403, "message": "YouTube Data API v3 has not been used in project",
                                             "errors": [{"reason": "accessNotConfigured"}]}})
        if u.path == "/search":
            assert q["type"] == "video" and q["videoCategoryId"] == "10" and q["order"] == "viewCount", q
            assert "publishedAfter" in q
            ids = list(VIDEOS)
            if q.get("videoDuration") == "long":
                ids = [i for i in ids if VIDEOS[i][2].startswith("PT1H") or VIDEOS[i][2] == "PT58M"]
            page2 = q.get("pageToken") == "P2"
            items = ids[3:] if page2 else ids[:3]
            out = {"items": [{"id": {"videoId": i}, "snippet": {"channelId": cid(VIDEOS[i][0])}} for i in items]}
            if not page2:
                out["nextPageToken"] = "P2"
            return self.send(200, out)
        if u.path == "/videos":
            items = []
            for i in q["id"].split(","):
                ch, views, dur, days = VIDEOS[i]
                items.append({"id": i, "snippet": {"title": f"Vídeo {i}", "channelId": cid(ch), "publishedAt": iso(days),
                                                    "thumbnails": {"medium": {"url": f"https://i.ytimg.com/{i}.jpg"}}},
                              "statistics": {"viewCount": str(views), "likeCount": "100"},
                              "contentDetails": {"duration": dur}})
            return self.send(200, {"items": items})
        if u.path == "/channels":
            items = []
            for c in q["id"].split(","):
                ch = next(k for k in CHANNELS if cid(k) == c)
                d = CHANNELS[ch]
                st = {"viewCount": d["views"], "videoCount": d["videos"]}
                if d["subs"] is None:
                    st["hiddenSubscriberCount"] = True
                else:
                    st["subscriberCount"] = d["subs"]
                items.append({"id": c, "snippet": {"title": d["title"], "publishedAt": iso(d["created"]),
                                                   "country": d["country"], "customUrl": "@" + d["title"].replace(" ", "").lower(),
                                                   "thumbnails": {"default": {"url": f"https://yt3/{ch}.jpg"}}},
                              "statistics": st})
            return self.send(200, {"items": items})
        self.send(404, {"error": {"message": "nope"}})


class Base(base.Base):
    @classmethod
    def setUpClass(cls):
        cls.fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeYT)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        b = f"http://127.0.0.1:{cls.fake.server_address[1]}"
        cls.env = {"ACCESS_PASSWORD": PASSWORD, "YOUTUBE_API_BASE": b, "YOUTUBE_WEB_BASE": b, **getattr(cls, "extra", {})}
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.fake.shutdown()

    def post(self, path, body, headers=AUTH):
        st, _, raw = base.request(self.base, path, body, headers=headers)
        return st, json.loads(raw)


class TestChannelFinder(Base):
    def test_1_settings(self):
        st, d = self.post("/api/channels/settings", {"api_key": "curta"})
        self.assertEqual(st, 400)
        st, d = self.post("/api/channels/settings", {"api_key": KEY}, headers={})
        self.assertEqual(st, 401)
        st, d = self.post("/api/channels/settings", {"api_key": KEY})
        self.assertEqual(st, 200, d)
        self.assertTrue(d["api_key_set"])
        self.assertNotIn(KEY, json.dumps(d))
        self.assertEqual(d["api_key_hint"], "AIza…abcd")
        self.assertIn("normal", d["options"]["depths"])

    def test_2_search_filters_and_metrics(self):
        self.post("/api/channels/settings", {"api_key": KEY})
        FakeYT.log.clear()
        st, d = self.post("/api/channels/search", {"depth": "rapida", "channel_days": 30, "video_days": 30, "min_views": 100000})
        self.assertEqual(st, 200, d)
        found = {c["title"]: c for c in d["channels"]}
        self.assertEqual(set(found), {"Nova Slow Rock", "Louvores de Paz", "Shorts Music"})  # C antigo, D poucas views
        a = found["Nova Slow Rock"]
        self.assertAlmostEqual(a["age_days"], 5, delta=0.1)
        self.assertAlmostEqual(a["views_per_day"], 600000, delta=100)
        self.assertAlmostEqual(a["subs_per_day"], 500, delta=1)
        self.assertEqual([v["id"] for v in a["videos"]], ["vidA1xxxxxx", "vidA2xxxxxx"])
        self.assertEqual(a["videos"][0]["duration"], 3730)
        self.assertEqual(a["monetization"]["status"], "provavel")
        self.assertEqual(found["Louvores de Paz"]["monetization"]["status"], "perto")
        self.assertEqual(found["Shorts Music"]["monetization"]["status"], "provavel")  # via Shorts (10 mi)
        self.assertTrue(found["Shorts Music"]["monetization"]["shorts"])
        scores = [c["score"] for c in d["channels"]]
        self.assertEqual(scores, sorted(scores, reverse=True))                           # melhor pontuação primeiro
        self.assertEqual(d["channels"][0]["title"], "Shorts Music")                      # mais views/dia (1,2 mi)
        # cota: 1 busca × 2 páginas (100 cada) + 1 videos + 1 channels
        self.assertEqual(d["stats"]["units"], 202)
        self.assertEqual(d["stats"]["videos"], 6)
        paths = [p for p, _ in FakeYT.log]
        self.assertEqual(paths.count("/search"), 2)
        # a mesma busca de novo vem do cache: não gasta cota
        st, d2 = self.post("/api/channels/search", {"depth": "rapida", "channel_days": 30, "video_days": 30, "min_views": 100000})
        self.assertEqual(d2["stats"]["units"], 0)
        self.assertGreater(d2["stats"]["cached_calls"], 0)
        self.assertEqual(len(d2["channels"]), 3)
        self.assertEqual(d2["quota"]["used"], d["quota"]["used"])

    def test_3_filters(self):
        self.post("/api/channels/settings", {"api_key": KEY})
        st, d = self.post("/api/channels/search", {"depth": "rapida", "channel_days": 7, "video_days": 30, "min_views": 100000})
        self.assertEqual([c["title"] for c in d["channels"]], ["Nova Slow Rock"])         # só o de 5 dias
        st, d = self.post("/api/channels/search", {"depth": "rapida", "channel_days": 30, "min_views": 0, "min_subs": 1000})
        self.assertEqual({c["title"] for c in d["channels"]}, {"Nova Slow Rock", "Shorts Music"})
        st, d = self.post("/api/channels/search", {"depth": "rapida", "channel_days": 30, "min_views": 0})
        tiny = next(c for c in d["channels"] if c["title"] == "Tiny Beats")
        self.assertIsNone(tiny["subs"])                                                    # inscritos ocultos
        self.assertEqual(tiny["monetization"]["status"], "desconhecido")
        FakeYT.log.clear()
        st, d = self.post("/api/channels/search", {"depth": "rapida", "duration": "long", "keywords": ["playlist"], "lang": "en"})
        q = [q for p, q in FakeYT.log if p == "/search"][0]
        self.assertEqual((q["videoDuration"], q["q"], q["relevanceLanguage"]), ("long", "playlist", "en"))
        self.assertEqual({c["title"] for c in d["channels"]}, {"Nova Slow Rock"})

    def test_4_sweep_plan_and_estimate(self):
        st, d = self.post("/api/channels/estimate", {"depth": "normal"})
        self.assertEqual((d["searches"], d["units"]), (8, 810))
        st, d = self.post("/api/channels/estimate", {"depth": "profunda", "keywords": ["lofi", "worship"]})
        self.assertEqual(d["searches"], 60)                                                # teto de segurança
        st, d = self.post("/api/channels/estimate", {"region": "BR"})
        self.assertEqual(d["searches"], 2)
        self.post("/api/channels/settings", {"api_key": KEY})
        FakeYT.log.clear()
        self.post("/api/channels/search", {"depth": "normal", "keywords": ["sad songs"]})
        regions = {q.get("regionCode", "") for p, q in FakeYT.log if p == "/search"}
        self.assertEqual(regions, {"", "US", "BR", "MX", "IN", "ID", "PH", "DE"})

    def test_5_api_errors(self):
        for key, text in [("AIzaSyBAD_xxxxxxxxxxxxxxxxxxxxxxxxxxxx", "inválida"),
                          ("AIzaSyQUOTA_xxxxxxxxxxxxxxxxxxxxxxxxxx", "cota diária"),
                          ("AIzaSyOFF_xxxxxxxxxxxxxxxxxxxxxxxxxxxx", "não está ativada")]:
            with self.subTest(key=key[:12]):
                self.post("/api/channels/settings", {"api_key": key})
                st, d = self.post("/api/channels/search", {"depth": "rapida", "keywords": [key[:10]]})
                self.assertIn(st, (400, 429), d)
                self.assertIn(text, d["error"])
        self.post("/api/channels/settings", {"clear_api_key": True})
        st, d = self.post("/api/channels/search", {"depth": "rapida"})
        self.assertEqual(st, 400)
        self.assertIn("chave da API", d["error"])

    def test_6_ad_check(self):
        st, d = self.post("/api/channels/adcheck", {"video_id": "vidA1xxxxxx"})
        self.assertEqual(d["status"], "anuncios")
        st, d = self.post("/api/channels/adcheck", {"video_id": "vidB1xxxxxx"})
        self.assertEqual(d["status"], "sem_anuncios")
        st, d = self.post("/api/channels/adcheck", {"video_id": "vidC1xxxxxx"})
        self.assertEqual(d["status"], "erro")
        st, d = self.post("/api/channels/adcheck", {"video_id": "../etc"})
        self.assertEqual(st, 400)

    def test_7_favorites(self):
        ch = {"id": cid("A"), "title": "Nova Slow Rock", "views": 3000000, "monetization": {"status": "provavel"}}
        st, d = self.post("/api/channels/favorites", ch)
        self.assertEqual([f["id"] for f in d["favorites"]], [cid("A")])
        self.post("/api/channels/favorites", {**ch, "note": "modelar thumbnails"})
        st, _, raw = base.request(self.base, "/api/channels/favorites", headers=AUTH)
        favs = json.loads(raw)["favorites"]
        self.assertEqual((len(favs), favs[0]["note"]), (1, "modelar thumbnails"))
        st, _, raw = base.request(self.base, f"/api/channels/favorites/{cid('A')}", method="DELETE", headers=AUTH)
        self.assertEqual(json.loads(raw)["favorites"], [])
        self.assertEqual(self.post("/api/channels/favorites", {"id": "x"})[0], 400)
        self.assertEqual(base.request(self.base, "/api/channels/favorites")[0], 401)


class TestQuotaGuard(Base):
    extra = {"YOUTUBE_DAILY_QUOTA": "500"}

    def test_blocks_search_bigger_than_remaining_quota(self):
        self.post("/api/channels/settings", {"api_key": KEY})
        FakeYT.log.clear()
        st, d = self.post("/api/channels/search", {"depth": "normal"})
        self.assertEqual(st, 429)
        self.assertIn("restam", d["error"])
        self.assertEqual(FakeYT.log, [])                                                   # nada foi gasto


class TestKeyNeedsPassword(base.Base):
    env = {}

    def test_key_requires_password(self):
        st, _, raw = base.request(self.base, "/api/channels/settings", {"api_key": KEY})
        self.assertEqual(st, 403)
        self.assertIn("SENHA_ACESSO", json.loads(raw)["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
