"""
Testes de ponta a ponta do YTB Conversor (sem internet).

Sobe o servidor de verdade com ALLOW_LOCAL_TEST=1 e usa um arquivo de áudio
gerado pelo ffmpeg no lugar do YouTube, validando cortes, formatos, erros,
senha e cookies. Uso:  python3 tests/test_e2e.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "app", "server.py")
TMP = tempfile.mkdtemp(prefix="ytbc-test-")
SOURCE = os.path.join(TMP, "Música de Teste ção.m4a")  # nome com acento de propósito
SOURCE_URL = "file://" + SOURCE
PASSWORD = "senha-teste-123"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(extra_env):
    port = free_port()
    env = dict(os.environ, PORT=str(port), ALLOW_LOCAL_TEST="1", DATA_DIR=tempfile.mkdtemp(dir=TMP),
               APP_VERSION="teste", **extra_env)
    proc = subprocess.Popen([sys.executable, SERVER], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            return proc, base
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("servidor não subiu: " + proc.stdout.read().decode())


def request(base, path, data=None, method=None, headers=None, raw=None):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(base + path, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def probe(data, suffix):
    path = os.path.join(TMP, "out" + suffix)
    with open(path, "wb") as fh:
        fh.write(data)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:stream=codec_name,sample_rate,channels", "-of", "json", path],
                         capture_output=True, text=True, check=True)
    info = json.loads(out.stdout)
    st = info["streams"][0]
    return float(info["format"]["duration"]), st["codec_name"], int(st["sample_rate"]), int(st["channels"])


class Base(unittest.TestCase):
    env = {}

    @classmethod
    def setUpClass(cls):
        cls.proc, cls.base = start_server(cls.env)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)


class TestDownloads(Base):
    env = {"MAX_CLIP_SECONDS": "200"}

    def test_config_defaults(self):
        st, _, body = request(self.base, "/api/config")
        cfg = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(cfg["defaults"]["start"], "00:00")
        self.assertEqual(cfg["defaults"]["end"], "02:00")
        self.assertEqual(cfg["defaults"]["format"], "wav")
        self.assertFalse(cfg["auth_required"])

    def test_index_served(self):
        st, hdrs, body = request(self.base, "/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", hdrs["Content-Type"])
        self.assertIn(b"YTB", body)

    def test_default_is_first_two_minutes_wav(self):
        st, hdrs, body = request(self.base, "/api/download", {"url": SOURCE_URL})
        self.assertEqual(st, 200, body[:300])
        self.assertEqual(hdrs["Content-Type"], "audio/wav")
        dur, codec, _, _ = probe(body, ".wav")
        self.assertAlmostEqual(dur, 120, delta=0.15)
        self.assertEqual(codec, "pcm_s16le")
        name = urllib.parse.unquote(hdrs["X-Filename"])
        self.assertEqual(name, "Música de Teste ção (00.00 a 02.00).wav")
        self.assertIn("filename*=UTF-8''", hdrs["Content-Disposition"])

    def test_all_formats_custom_range(self):
        expected = {"wav": "pcm_s16le", "mp3": "mp3", "m4a": "aac", "flac": "flac", "ogg": "vorbis", "opus": "opus"}
        for fmt, codec in expected.items():
            with self.subTest(fmt=fmt):
                st, hdrs, body = request(self.base, "/api/download",
                                         {"url": SOURCE_URL, "start": "00:30", "end": "0:45", "format": fmt, "bitrate": 128})
                self.assertEqual(st, 200, body[:300])
                dur, got, _, _ = probe(body, "." + fmt)
                self.assertAlmostEqual(dur, 15, delta=0.2)
                self.assertEqual(got, codec)

    def test_hh_mm_ss_and_seconds(self):
        st, _, body = request(self.base, "/api/download", {"url": SOURCE_URL, "start": "00:01:00", "end": "70", "format": "mp3"})
        self.assertEqual(st, 200, body[:300])
        self.assertAlmostEqual(probe(body, ".mp3")[0], 10, delta=0.2)

    def test_end_beyond_video_is_clamped(self):
        st, _, body = request(self.base, "/api/download", {"url": SOURCE_URL, "start": "04:50", "end": "09:00"})
        self.assertEqual(st, 200, body[:300])
        self.assertAlmostEqual(probe(body, ".wav")[0], 10, delta=0.2)

    def test_empty_end_goes_to_end(self):
        st, _, body = request(self.base, "/api/download", {"url": SOURCE_URL, "start": "04:00", "end": ""})
        self.assertEqual(st, 200, body[:300])
        self.assertAlmostEqual(probe(body, ".wav")[0], 60, delta=0.2)

    def test_samplerate_and_mono(self):
        st, _, body = request(self.base, "/api/download",
                              {"url": SOURCE_URL, "end": "00:05", "samplerate": "16000", "channels": "1"})
        self.assertEqual(st, 200, body[:300])
        _, _, sr, ch = probe(body, ".wav")
        self.assertEqual((sr, ch), (16000, 1))

    def test_custom_filename(self):
        st, hdrs, _ = request(self.base, "/api/download",
                              {"url": SOURCE_URL, "end": "00:03", "format": "mp3", "filename": "meu/trecho.mp3"})
        self.assertEqual(st, 200)
        self.assertEqual(urllib.parse.unquote(hdrs["X-Filename"]), "meu trecho.mp3")

    def test_get_for_n8n(self):
        q = urllib.parse.urlencode({"url": SOURCE_URL, "start": "10", "end": "20", "format": "mp3"})
        st, hdrs, body = request(self.base, "/api/download?" + q)
        self.assertEqual(st, 200, body[:300])
        self.assertEqual(hdrs["Content-Type"], "audio/mpeg")
        self.assertAlmostEqual(probe(body, ".mp3")[0], 10, delta=0.2)

    def test_info(self):
        st, _, body = request(self.base, "/api/info", {"url": SOURCE_URL})
        data = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(data["duration_text"], "05:00")

    def test_errors(self):
        cases = [
            ({"url": SOURCE_URL, "start": "01:00", "end": "00:30"}, 400, "maior"),
            ({"url": SOURCE_URL, "format": "exe"}, 400, "Formato"),
            ({"url": SOURCE_URL, "start": "1:75"}, 400, "59"),
            ({"url": SOURCE_URL, "start": "abc"}, 400, "inválido"),
            ({"url": SOURCE_URL, "start": "06:00", "end": ""}, 400, "depois do fim"),
            ({"url": SOURCE_URL, "start": "0", "end": "04:00"}, 400, "máximo"),
            ({"url": "https://exemplo.com/video"}, 400, "YouTube"),
            ({"url": "http://localhost:5678/webhook"}, 400, "YouTube"),
            ({"url": ""}, 400, "link"),
            ({"url": SOURCE_URL, "bitrate": 999}, 400, "Qualidade"),
        ]
        for payload, status, text in cases:
            with self.subTest(payload=payload):
                st, _, body = request(self.base, "/api/download", payload)
                self.assertEqual(st, status, body)
                self.assertIn(text, json.loads(body)["error"])

    def test_cookies_need_password(self):
        st, _, _ = request(self.base, "/api/cookies", raw=b".youtube.com\tTRUE\t/\tTRUE\t0\tA\tB\n", method="POST")
        self.assertEqual(st, 403)


class TestAuth(Base):
    env = {"ACCESS_PASSWORD": PASSWORD}

    def test_requires_password(self):
        st, _, _ = request(self.base, "/api/download", {"url": SOURCE_URL, "end": "00:02"})
        self.assertEqual(st, 401)
        st, _, _ = request(self.base, "/api/download", {"url": SOURCE_URL, "end": "00:02"}, headers={"X-Access-Key": "errada"})
        self.assertEqual(st, 401)
        st, _, _ = request(self.base, "/api/download", {"url": SOURCE_URL, "end": "00:02"}, headers={"X-Access-Key": PASSWORD})
        self.assertEqual(st, 200)
        q = urllib.parse.urlencode({"url": SOURCE_URL, "end": "2", "key": PASSWORD})
        self.assertEqual(request(self.base, "/api/download?" + q)[0], 200)

    def test_page_and_config_public(self):
        self.assertEqual(request(self.base, "/")[0], 200)
        cfg = json.loads(request(self.base, "/api/config")[2])
        self.assertTrue(cfg["auth_required"])

    def test_login(self):
        self.assertEqual(request(self.base, "/api/login", {"key": PASSWORD})[0], 200)
        self.assertEqual(request(self.base, "/api/login", {"key": "x"})[0], 401)

    def test_cookies_upload_and_delete(self):
        cookie = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tx\n"
        self.assertEqual(request(self.base, "/api/cookies", raw=cookie, method="POST")[0], 401)
        st, _, body = request(self.base, "/api/cookies", raw=b"lixo", method="POST", headers={"X-Access-Key": PASSWORD})
        self.assertEqual(st, 400, body)
        st, _, _ = request(self.base, "/api/cookies", raw=cookie, method="POST", headers={"X-Access-Key": PASSWORD})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(request(self.base, "/api/config")[2])["cookies_configured"])
        st, _, _ = request(self.base, "/api/cookies", method="DELETE", headers={"X-Access-Key": PASSWORD})
        self.assertEqual(st, 200)
        self.assertFalse(json.loads(request(self.base, "/api/config")[2])["cookies_configured"])


def setUpModule():
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=300",
                    "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=44100:duration=300",
                    "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]", "-map", "[a]",
                    "-c:a", "aac", "-b:a", "128k", SOURCE], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
