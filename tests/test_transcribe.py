"""
Testes da transcrição (python3 tests/test_transcribe.py).

- Motor OpenAI: contra um servidor falso local (sem internet, sem custo),
  validando envio em partes, divisão do áudio em silêncios, prompt de
  continuidade, idioma e mensagens de erro.
- Motor gratuito (faster-whisper): só roda se o pacote e o espeak-ng
  estiverem instalados (no GitHub Actions); transcreve voz sintetizada.
"""

import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_e2e as base  # reaproveita servidor e helpers

TMP = tempfile.mkdtemp(prefix="ytbc-tr-")
PASSWORD = "senha-tr-123"
GOOD_KEY = "sk-teste-0123456789abcdefghij"
BAD_KEY = "sk-recusada-0123456789abcdefgh"
AUTH = {"X-Access-Key": PASSWORD}


class FakeOpenAI(http.server.BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        fields = dict(re.findall(rb'name="(\w+)"\r\n\r\n(.*?)\r\n', body, re.S))
        audio = body.split(b"Content-Type: audio/mpeg\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
        path = os.path.join(TMP, f"recebido{len(FakeOpenAI.calls)}.mp3")
        open(path, "wb").write(audio)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                    "default=nw=1:nk=1", path], capture_output=True, text=True).stdout or 0)
        FakeOpenAI.calls.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                 "fields": {k.decode(): v.decode() for k, v in fields.items()}, "dur": dur})
        if self.headers.get("Authorization") == "Bearer " + BAD_KEY:
            out, code = {"error": {"message": "Incorrect API key", "code": "invalid_api_key"}}, 401
        else:
            out, code = {"text": f"parte {len(FakeOpenAI.calls)}."}, 200
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def wait_job(base_url, job_id, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, _, body = base.request(base_url, "/api/jobs/" + job_id, headers=AUTH)
        job = json.loads(body)["job"]
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.3)
    raise AssertionError("transcrição não terminou")


def upload(base_url, path, chunk=None):
    size = os.path.getsize(path)
    st, _, body = base.request(base_url, "/api/upload/init", {"name": os.path.basename(path), "size": size}, headers=AUTH)
    assert st == 200, body
    uid = json.loads(body)["upload_id"]
    data = open(path, "rb").read()
    chunk = chunk or size
    for off in range(0, size, chunk):
        st, _, body = base.request(base_url, f"/api/upload/{uid}?offset={off}", raw=data[off:off + chunk],
                                   method="PUT", headers=AUTH)
        assert st == 200, body
    return uid


class TestOpenAIEngine(base.Base):
    @classmethod
    def setUpClass(cls):
        cls.fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.env = {"ACCESS_PASSWORD": PASSWORD, "OPENAI_CHUNK_SECONDS": "60",
                   "OPENAI_BASE_URL": f"http://127.0.0.1:{cls.fake.server_address[1]}",
                   "UPLOAD_DIR": tempfile.mkdtemp(dir=TMP)}
        super().setUpClass()
        # 150 s de "fala" (tom) com 2 s de silêncio a cada 20 s
        cls.audio = os.path.join(TMP, "reuniao gravada.mp4")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "aevalsrc='sin(440*2*PI*t)*lt(mod(t,20),18)':s=44100:d=150", "-c:a", "aac", cls.audio], check=True)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.fake.shutdown()

    def setUp(self):
        FakeOpenAI.calls.clear()

    def save(self, payload, headers=AUTH):
        return base.request(self.base, "/api/transcribe/settings", payload, headers=headers)

    def test_1_settings_validation(self):
        st, _, body = base.request(self.base, "/api/transcribe/settings")
        self.assertEqual(st, 401)
        st, _, body = base.request(self.base, "/api/transcribe/settings", headers=AUTH)
        s = json.loads(body)
        self.assertEqual((s["engine"], s["local_model"], s["openai_model"], s["language"]),
                         ("local", "small", "gpt-4o-mini-transcribe", "pt"))
        self.assertFalse(s["openai_key_set"])
        self.assertEqual(self.save({"engine": "openai"})[0], 400)            # sem chave
        self.assertEqual(self.save({"openai_key": "abc"})[0], 400)           # formato inválido
        self.assertEqual(self.save({"local_model": "gigante"})[0], 400)
        self.assertEqual(self.save({"engine": "openai", "openai_key": GOOD_KEY}, headers={})[0], 401)

    def test_2_openai_chunked_upload_and_split(self):
        st, _, body = self.save({"engine": "openai", "openai_key": GOOD_KEY})
        s = json.loads(body)
        self.assertEqual(st, 200, body)
        self.assertTrue(s["openai_key_set"])
        self.assertNotIn(GOOD_KEY, body.decode())                            # chave nunca volta inteira
        self.assertEqual(s["openai_key_hint"], "sk-…ghij")

        # envio em 3 partes + reenvio da 1ª (nova tentativa) + parte fora de ordem
        size = os.path.getsize(self.audio)
        st, _, body = base.request(self.base, "/api/upload/init", {"name": "reuniao gravada.mp4", "size": size}, headers=AUTH)
        uid = json.loads(body)["upload_id"]
        data = open(self.audio, "rb").read()
        part = size // 3 + 1
        put = lambda off, raw: base.request(self.base, f"/api/upload/{uid}?offset={off}", raw=raw, method="PUT", headers=AUTH)
        self.assertEqual(put(0, data[:part])[0], 200)
        self.assertEqual(put(0, data[:part])[0], 200)                        # reenvio ok
        self.assertEqual(put(part * 2, data[part * 2:])[0], 409)             # pulou parte
        st, _, body = base.request(self.base, "/api/transcribe", {"upload_id": uid}, headers=AUTH)
        self.assertEqual(st, 400, body)                                      # incompleto
        self.assertEqual(put(part, data[part:part * 2])[0], 200)
        st, _, body = put(part * 2, data[part * 2:])
        self.assertTrue(json.loads(body)["complete"])

        st, _, body = base.request(self.base, "/api/transcribe", {"upload_id": uid}, headers=AUTH)
        self.assertEqual(st, 200, body)
        job = wait_job(self.base, json.loads(body)["job"]["id"])
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["text"], "parte 1. parte 2. parte 3.")
        self.assertAlmostEqual(job["duration"], 150, delta=0.5)
        self.assertEqual(job["name"], "reuniao gravada.mp4")

        calls = FakeOpenAI.calls
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["path"], "/v1/audio/transcriptions")
        self.assertEqual(calls[0]["auth"], "Bearer " + GOOD_KEY)
        self.assertEqual(calls[0]["fields"]["model"], "gpt-4o-mini-transcribe")
        self.assertEqual(calls[0]["fields"]["language"], "pt")
        self.assertNotIn("prompt", calls[0]["fields"])
        self.assertEqual(calls[1]["fields"]["prompt"], "parte 1.")          # continuidade
        # cortes nos silêncios (~59 s e ~119 s), não no meio da fala
        self.assertAlmostEqual(calls[0]["dur"], 59, delta=1.2)
        self.assertAlmostEqual(calls[1]["dur"], 60, delta=1.2)
        self.assertAlmostEqual(sum(c["dur"] for c in calls), 150, delta=1.5)
        # arquivo enviado é apagado depois
        st, _, body = base.request(self.base, "/api/transcribe", {"upload_id": uid}, headers=AUTH)
        self.assertEqual(st, 404)

    def test_3_language_auto_and_youtube_link_path(self):
        self.save({"engine": "openai", "openai_key": GOOD_KEY})
        short = os.path.join(TMP, "curto.wav")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=d=20", short], check=True)
        st, _, body = base.request(self.base, "/api/transcribe", {"url": "file://" + short, "language": "auto"}, headers=AUTH)
        self.assertEqual(st, 200, body)
        job = wait_job(self.base, json.loads(body)["job"]["id"])
        self.assertEqual((job["status"], job["text"]), ("done", "parte 1."))
        self.assertNotIn("language", FakeOpenAI.calls[0]["fields"])
        self.assertEqual(len(FakeOpenAI.calls), 1)

    def test_4_errors(self):
        self.save({"engine": "openai", "openai_key": BAD_KEY})
        uid = upload(self.base, self.audio)
        job = wait_job(self.base, json.loads(base.request(self.base, "/api/transcribe", {"upload_id": uid}, headers=AUTH)[2])["job"]["id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("recusou a chave", job["error"])

        lixo = os.path.join(TMP, "nao-e-video.mp4")
        open(lixo, "wb").write(b"isto nao e um video" * 100)
        uid = upload(self.base, lixo)
        job = wait_job(self.base, json.loads(base.request(self.base, "/api/transcribe", {"upload_id": uid}, headers=AUTH)[2])["job"]["id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("Não consegui ler esse arquivo", job["error"])

        self.assertEqual(base.request(self.base, "/api/upload/init", {"name": "x", "size": 10**13}, headers=AUTH)[0], 413)
        self.assertEqual(base.request(self.base, "/api/transcribe", {}, headers=AUTH)[0], 400)
        self.assertEqual(base.request(self.base, "/api/jobs/" + "0" * 32, headers=AUTH)[0], 404)
        self.assertEqual(base.request(self.base, "/api/upload/" + "a" * 32 + "?offset=0", raw=b"x", method="PUT", headers=AUTH)[0], 404)
        self.assertEqual(base.request(self.base, "/api/upload/init", {"name": "x", "size": 10})[0], 401)

    def test_5_clear_key(self):
        self.save({"engine": "openai", "openai_key": GOOD_KEY})
        st, _, body = self.save({"engine": "local", "clear_openai_key": True})
        self.assertFalse(json.loads(body)["openai_key_set"])


class TestKeyNeedsPassword(base.Base):
    env = {}

    def test_key_requires_password(self):
        st, _, body = base.request(self.base, "/api/transcribe/settings", {"openai_key": GOOD_KEY})
        self.assertEqual(st, 403, body)
        self.assertIn("SENHA_ACESSO", json.loads(body)["error"])


def _has_local_engine():
    try:
        import faster_whisper  # noqa
    except ImportError:
        return False
    return shutil.which("espeak-ng") is not None


@unittest.skipUnless(_has_local_engine(), "faster-whisper/espeak-ng não instalados (roda no GitHub Actions)")
class TestLocalEngine(base.Base):
    env = {"ACCESS_PASSWORD": PASSWORD, "UPLOAD_DIR": TMP}

    def test_real_speech(self):
        model = os.environ.get("TEST_WHISPER_MODEL", "base")
        st, _, body = base.request(self.base, "/api/transcribe/settings", {"engine": "local", "local_model": model}, headers=AUTH)
        self.assertEqual(st, 200, body)
        wav = os.path.join(TMP, "fala.wav")
        frase = "Bom dia a todos. Hoje vamos falar sobre consórcio e planejamento financeiro para comprar a casa própria."
        subprocess.run(["espeak-ng", "-v", "pt-br", "-s", "140", "-w", wav, frase], check=True)
        video = os.path.join(TMP, "fala.mp4")  # vídeo de verdade: imagem + voz
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x240:r=10",
                        "-i", wav, "-shortest", "-c:v", "mpeg4", "-c:a", "aac", video], check=True)
        uid = upload(self.base, video, chunk=40000)
        st, _, body = base.request(self.base, "/api/transcribe", {"upload_id": uid}, headers=AUTH)
        job = wait_job(self.base, json.loads(body)["job"]["id"], timeout=900)
        print("\nTRANSCRICAO_LOCAL:", job.get("text") or job.get("error"))
        self.assertEqual(job["status"], "done", job)
        text = job["text"].lower()
        hits = [w for w in ["bom dia", "todos", "consórcio", "planejamento", "financeiro", "casa"] if w in text]
        self.assertGreaterEqual(len(hits), 3, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
