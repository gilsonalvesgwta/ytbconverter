"""
YTB Conversor — baixa trechos de áudio de vídeos do YouTube.

Servidor HTTP sem dependências além do yt-dlp (para ler o vídeo) e do ffmpeg
(para cortar e converter). Serve a interface web e uma API simples que
também pode ser chamada pelo n8n.
"""

import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import transcribe as tr

# ---------------------------------------------------------------- config
PORT = int(os.environ.get("PORT", "8080"))
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "").strip()
MAX_CLIP_SECONDS = int(os.environ.get("MAX_CLIP_SECONDS", "3600"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "900"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
COOKIES_FILE = os.environ.get("COOKIES_FILE", os.path.join(DATA_DIR, "cookies.txt"))
ALLOW_LOCAL_TEST = os.environ.get("ALLOW_LOCAL_TEST", "") == "1"  # só para testes automatizados
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VERSION = os.environ.get("APP_VERSION", "dev")

DEFAULTS = {"start": "00:00", "end": "02:00", "format": "wav", "bitrate": 192,
            "samplerate": "", "channels": ""}

FORMATS = {
    "wav":  {"ext": "wav",  "mime": "audio/wav",  "codec": ["-c:a", "pcm_s16le"], "lossy": False, "label": "WAV (sem perda)"},
    "mp3":  {"ext": "mp3",  "mime": "audio/mpeg", "codec": ["-c:a", "libmp3lame"], "lossy": True, "label": "MP3"},
    "m4a":  {"ext": "m4a",  "mime": "audio/mp4",  "codec": ["-c:a", "aac", "-movflags", "+faststart"], "lossy": True, "label": "M4A (AAC)"},
    "flac": {"ext": "flac", "mime": "audio/flac", "codec": ["-c:a", "flac"], "lossy": False, "label": "FLAC (sem perda)"},
    "ogg":  {"ext": "ogg",  "mime": "audio/ogg",  "codec": ["-c:a", "libvorbis"], "lossy": True, "label": "OGG (Vorbis)"},
    "opus": {"ext": "opus", "mime": "audio/ogg",  "codec": ["-c:a", "libopus"], "lossy": True, "label": "OPUS"},
}
BITRATES = [64, 96, 128, 160, 192, 256, 320]
SAMPLERATES = ["", "16000", "22050", "44100", "48000"]
CHANNELS = ["", "1", "2"]

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("ytbconversor")

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_info_cache = {}
_info_lock = threading.Lock()
INFO_TTL = 600


class UserError(Exception):
    """Erro com mensagem em português, mostrada ao usuário."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- helpers
def parse_time(value, field):
    """Aceita '90', '1:30', '01:30', '00:01:30', '1:30.5'. Devolve segundos."""
    if value is None or str(value).strip() == "":
        raise UserError(f"Informe o tempo de {field}.")
    text = str(value).strip().replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?(:\d{1,2}(\.\d+)?){0,2}", text):
        raise UserError(f"Tempo de {field} inválido: use mm:ss ou hh:mm:ss (ex.: 01:30).")
    parts = [float(p) for p in text.split(":")]
    if len(parts) > 1 and any(p >= 60 for p in parts[1:]):
        raise UserError(f"Tempo de {field} inválido: minutos e segundos vão até 59.")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def fmt_time(seconds, sep=":"):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}{sep}{m:02d}{sep}{s:02d}"
    return f"{m:02d}{sep}{s:02d}"


def validate_url(url):
    url = (url or "").strip()
    if not url:
        raise UserError("Cole o link do vídeo do YouTube.")
    if ALLOW_LOCAL_TEST and url.startswith("file://"):
        return url
    parsed = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS):
        raise UserError("O link precisa ser de um vídeo do YouTube (youtube.com ou youtu.be).")
    return parsed.geturl()


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:120] or "audio"


def friendly_ytdlp_error(exc):
    msg = str(exc)
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return UserError("O YouTube pediu verificação de robô para o servidor. Envie o arquivo de cookies em "
                         "Configurações → Cookies do YouTube (veja o guia no README).", 502)
    if "private video" in low:
        return UserError("Este vídeo é privado.", 422)
    if "age" in low and ("restrict" in low or "confirm your age" in low):
        return UserError("Vídeo com restrição de idade: envie o arquivo de cookies de uma conta logada.", 422)
    if "video unavailable" in low or "is not available" in low or "has been removed" in low:
        return UserError("Vídeo indisponível ou removido.", 422)
    if "live event" in low or "premieres in" in low:
        return UserError("Transmissão ao vivo ainda não começou.", 422)
    if "unsupported url" in low or "incomplete youtube id" in low:
        return UserError("Não reconheci esse link como um vídeo do YouTube.", 422)
    return UserError("Não consegui ler o vídeo no YouTube. Detalhe: " + msg.splitlines()[-1][:300], 502)


def _cookie_copy():
    """yt-dlp regrava o arquivo de cookies; usamos uma cópia temporária."""
    if not os.path.isfile(COOKIES_FILE):
        return None
    fd, path = tempfile.mkstemp(prefix=tr.WORK_PREFIX + "cookies-", suffix=".txt")
    os.close(fd)
    shutil.copyfile(COOKIES_FILE, path)
    return path


def _ydl_base_opts():
    return {"quiet": True, "no_warnings": True, "noplaylist": True, "playlist_items": "1",
            "socket_timeout": 30, "retries": 3}


def extract_info(url):
    """Lê título, duração e link do áudio. Usa cache de 10 min."""
    now = time.time()
    with _info_lock:
        hit = _info_cache.get(url)
        if hit and now - hit["_t"] < INFO_TTL:
            return hit
        for k in [k for k, v in _info_cache.items() if now - v["_t"] >= INFO_TTL]:
            del _info_cache[k]

    if ALLOW_LOCAL_TEST and url.startswith("file://"):
        path = urllib.parse.unquote(url[len("file://"):])
        if not os.path.isfile(path):
            raise UserError("Arquivo de teste não encontrado.", 404)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=nw=1:nk=1", path], capture_output=True, text=True)
        info = {"id": "local", "title": os.path.splitext(os.path.basename(path))[0],
                "duration": float(out.stdout.strip() or 0), "thumbnail": "", "uploader": "teste",
                "stream_url": path, "headers": {}}
    else:
        import yt_dlp  # importado aqui para o servidor subir mesmo antes da atualização

        cookies = _cookie_copy()
        opts = _ydl_base_opts()
        opts["format"] = "bestaudio/best"
        if cookies:
            opts["cookiefile"] = cookies
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise friendly_ytdlp_error(exc)
        finally:
            if cookies:
                os.unlink(cookies)
        if raw.get("entries"):
            raw = next(iter(raw["entries"]), None) or raw
        if raw.get("is_live"):
            raise UserError("Transmissões ao vivo não são suportadas.", 422)
        stream = raw.get("url")
        headers = raw.get("http_headers") or {}
        if not stream and raw.get("requested_formats"):
            fmt = raw["requested_formats"][0]
            stream, headers = fmt.get("url"), fmt.get("http_headers") or headers
        info = {"id": raw.get("id"), "title": raw.get("title") or "audio",
                "duration": float(raw.get("duration") or 0), "thumbnail": raw.get("thumbnail") or "",
                "uploader": raw.get("uploader") or raw.get("channel") or "",
                "stream_url": stream, "headers": headers, "webpage_url": raw.get("webpage_url") or url}

    info["_t"] = now
    with _info_lock:
        _info_cache[url] = info
    return info


def parse_options(params):
    fmt = str(params.get("format") or DEFAULTS["format"]).lower().strip(".")
    if fmt not in FORMATS:
        raise UserError("Formato inválido. Use: " + ", ".join(FORMATS))
    start = parse_time(params.get("start", DEFAULTS["start"]) or DEFAULTS["start"], "início")
    end_raw = params.get("end", DEFAULTS["end"])
    end = None if str(end_raw).strip().lower() in ("", "fim", "end", "max") else parse_time(end_raw, "fim")
    try:
        bitrate = int(params.get("bitrate") or DEFAULTS["bitrate"])
    except ValueError:
        raise UserError("Qualidade (kbps) inválida.")
    if bitrate not in BITRATES:
        raise UserError("Qualidade inválida. Use: " + ", ".join(map(str, BITRATES)) + " kbps.")
    samplerate = str(params.get("samplerate") or "")
    if samplerate not in SAMPLERATES:
        raise UserError("Taxa de amostragem inválida.")
    channels = str(params.get("channels") or "")
    if channels not in CHANNELS:
        raise UserError("Canais inválidos (use 1 = mono ou 2 = estéreo).")
    name = safe_filename(params.get("filename") or "") if params.get("filename") else ""
    return {"format": fmt, "start": start, "end": end, "bitrate": bitrate,
            "samplerate": samplerate, "channels": channels, "filename": name}


def encode_args(opts):
    spec = FORMATS[opts["format"]]
    args = ["-vn", "-sn", "-dn"] + spec["codec"]
    if spec["lossy"]:
        args += ["-b:a", f"{min(opts['bitrate'], 256) if opts['format'] == 'opus' else opts['bitrate']}k"]
    if opts["samplerate"]:
        args += ["-ar", opts["samplerate"]]
    elif opts["format"] == "opus":
        args += ["-ar", "48000"]
    if opts["channels"]:
        args += ["-ac", opts["channels"]]
    return args


def run_ffmpeg(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-800:] or "ffmpeg falhou")


def make_clip(url, opts, workdir):
    info = extract_info(url)
    duration = info.get("duration") or 0
    start, end = opts["start"], opts["end"]
    if end is None:
        if not duration:
            raise UserError("Não sei a duração do vídeo: informe o tempo de fim.")
        end = duration
    if duration:
        if start >= duration:
            raise UserError(f"O início ({fmt_time(start)}) é depois do fim do vídeo ({fmt_time(duration)}).")
        end = min(end, duration)
    if end <= start:
        raise UserError("O tempo de fim precisa ser maior que o de início.")
    length = end - start
    if length > MAX_CLIP_SECONDS:
        raise UserError(f"O trecho tem {fmt_time(length)}; o máximo permitido é {fmt_time(MAX_CLIP_SECONDS)}.")

    spec = FORMATS[opts["format"]]
    out = os.path.join(workdir, "saida." + spec["ext"])
    stream = info["stream_url"]
    if not stream:
        raise UserError("O YouTube não entregou o áudio deste vídeo.", 502)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if stream.startswith("http"):
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in (info.get("headers") or {}).items())
        if hdrs:
            cmd += ["-headers", hdrs]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    cmd += ["-ss", f"{start:.3f}", "-i", stream, "-t", f"{length:.3f}", "-map", "0:a:0"] + encode_args(opts) + [out]

    t0 = time.time()
    try:
        run_ffmpeg(cmd)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        if not stream.startswith("http"):
            raise
        log.warning("corte direto falhou (%s); tentando pelo yt-dlp", str(exc)[:200])
        with _info_lock:
            _info_cache.pop(url, None)
        fallback_clip(url, start, end, opts, workdir, out)

    if not os.path.isfile(out) or os.path.getsize(out) < 100:
        raise UserError("O arquivo de áudio saiu vazio. Confira o intervalo escolhido.", 502)
    log.info("ok %s [%s-%s] %s %.1fs %d bytes", info.get("id"), fmt_time(start), fmt_time(end),
             opts["format"], time.time() - t0, os.path.getsize(out))

    if opts["filename"]:
        base = re.sub(r"\.(wav|mp3|m4a|flac|ogg|opus)$", "", opts["filename"], flags=re.I)
    else:
        base = safe_filename(f"{info['title']} ({fmt_time(start, '.')} a {fmt_time(end, '.')})")
    return out, f"{base}.{spec['ext']}", spec["mime"]


def fallback_clip(url, start, end, opts, workdir, out):
    """Plano B: o próprio yt-dlp baixa só o trecho; depois convertemos."""
    import yt_dlp
    from yt_dlp.utils import download_range_func

    cookies = _cookie_copy()
    ydl_opts = _ydl_base_opts()
    ydl_opts.update({"format": "bestaudio/best", "outtmpl": os.path.join(workdir, "fonte.%(ext)s"),
                     "download_ranges": download_range_func(None, [(start, end)])})
    if cookies:
        ydl_opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise friendly_ytdlp_error(exc)
    finally:
        if cookies:
            os.unlink(cookies)
    sources = [f for f in os.listdir(workdir) if f.startswith("fonte.")]
    if not sources:
        raise UserError("Não consegui baixar o trecho do YouTube.", 502)
    src = os.path.join(workdir, sources[0])
    run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", src,
                "-map", "0:a:0"] + encode_args(opts) + [out])


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "YTBConversor/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # logs mais limpos
        if not self.path.startswith("/api/health"):
            log.info("%s %s", self.address_string(), fmt % args)

    # ---- respostas
    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False))

    def _error(self, status, message):
        self._json(status, {"ok": False, "error": message})

    # ---- entrada
    def _query(self):
        return {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    def _body(self, limit=2_000_000):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise UserError("Conteúdo grande demais.", 413)
        return self.rfile.read(length) if length else b""

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise UserError("JSON inválido.")
        if not isinstance(data, dict):
            raise UserError("JSON inválido.")
        return data

    def _authorized(self, params=None):
        if not ACCESS_PASSWORD:
            return True
        given = self.headers.get("X-Access-Key") or (params or {}).get("key") or ""
        return hmac.compare_digest(given.encode(), ACCESS_PASSWORD.encode())

    # ---- rotas
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path == "/favicon.svg":
                return self._static("favicon.svg")
            if path == "/api/health":
                return self._json(200, {"ok": True, "version": VERSION})
            if path == "/api/config":
                return self._json(200, self._config())
            params = self._query()
            if path == "/api/info":
                return self._info(params)
            if path == "/api/download":
                return self._download(params)
            if path == "/api/transcribe/settings":
                self._require_auth(params)
                return self._json(200, {"ok": True, **tr.public_settings()})
            if path.startswith("/api/jobs/"):
                self._require_auth(params)
                job = tr.get_job(path.rsplit("/", 1)[-1])
                if not job:
                    raise UserError("Transcrição não encontrada (o servidor pode ter reiniciado).", 404)
                return self._json(200, {"ok": True, "job": job})
            self._error(404, "Página não encontrada.")
        except (UserError, tr.TranscribeError) as exc:
            self._error(exc.status, str(exc))
        except Exception:
            log.exception("erro inesperado")
            self._error(500, "Erro interno. Veja os registros (logs) do serviço.")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/info":
                return self._info(self._json_body())
            if path == "/api/download":
                return self._download(self._json_body())
            if path == "/api/login":
                body = self._json_body()
                ok = not ACCESS_PASSWORD or hmac.compare_digest(str(body.get("key", "")).encode(), ACCESS_PASSWORD.encode())
                return self._json(200 if ok else 401, {"ok": ok} if ok else {"ok": False, "error": "Senha incorreta."})
            if path == "/api/cookies":
                return self._save_cookies()
            if path == "/api/transcribe/settings":
                self._require_auth()
                return self._json(200, {"ok": True, **tr.update_settings(self._json_body(), bool(ACCESS_PASSWORD))})
            if path == "/api/upload/init":
                self._require_auth()
                body = self._json_body()
                return self._json(200, {"ok": True, **tr.upload_init(body.get("name"), body.get("size"))})
            if path == "/api/transcribe":
                self._require_auth()
                return self._start_transcription(self._json_body())
            self._error(404, "Rota não encontrada.")
        except (UserError, tr.TranscribeError) as exc:
            self._error(exc.status, str(exc))
        except Exception:
            log.exception("erro inesperado")
            self._error(500, "Erro interno. Veja os registros (logs) do serviço.")

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            m = re.fullmatch(r"/api/upload/([0-9a-f]{32})", path)
            if not m:
                return self._error(404, "Rota não encontrada.")
            params = self._query()
            self._require_auth(params)
            length = int(self.headers.get("Content-Length") or 0)
            return self._json(200, {"ok": True, **tr.upload_chunk(m.group(1), params.get("offset", 0), self.rfile, length)})
        except (UserError, tr.TranscribeError) as exc:
            self.close_connection = True
            self._error(exc.status, str(exc))
        except Exception:
            log.exception("erro inesperado")
            self.close_connection = True
            self._error(500, "Erro interno. Veja os registros (logs) do serviço.")

    def _require_auth(self, params=None):
        if not self._authorized(params):
            raise UserError("Senha de acesso necessária.", 401)

    def _start_transcription(self, body):
        language = body.get("language") or None
        if body.get("upload_id"):
            job = tr.create_job({"upload_id": str(body["upload_id"])}, language)
        elif body.get("url"):
            url = validate_url(body.get("url"))

            def resolver():
                try:
                    info = extract_info(url)
                except UserError as exc:
                    raise tr.TranscribeError(str(exc))
                if not info.get("stream_url"):
                    raise tr.TranscribeError("O YouTube não entregou o áudio deste vídeo.")
                return info["stream_url"], info.get("headers") or {}, info.get("title") or "vídeo"

            job = tr.create_job({"url": url, "resolver": resolver}, language)
        else:
            raise UserError("Escolha um arquivo ou cole um link do YouTube.")
        return self._json(200, {"ok": True, "job": job})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/cookies":
            return self._error(404, "Rota não encontrada.")
        if not ACCESS_PASSWORD or not self._authorized():
            return self._error(401, "Senha de acesso necessária.")
        if os.path.isfile(COOKIES_FILE):
            os.unlink(COOKIES_FILE)
        self._json(200, {"ok": True})

    def _static(self, name):
        path = os.path.join(STATIC_DIR, name)
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else "image/svg+xml"
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _config(self):
        return {"ok": True, "version": VERSION, "auth_required": bool(ACCESS_PASSWORD),
                "defaults": DEFAULTS, "max_clip_seconds": MAX_CLIP_SECONDS,
                "formats": [{"id": k, "label": v["label"], "lossy": v["lossy"]} for k, v in FORMATS.items()],
                "bitrates": BITRATES, "samplerates": SAMPLERATES,
                "cookies_configured": os.path.isfile(COOKIES_FILE),
                "cookies_editable": bool(ACCESS_PASSWORD)}

    def _info(self, params):
        if not self._authorized(params):
            return self._error(401, "Senha de acesso necessária.")
        url = validate_url(params.get("url"))
        info = extract_info(url)
        self._json(200, {"ok": True, "id": info.get("id"), "title": info["title"], "duration": info["duration"],
                         "duration_text": fmt_time(info["duration"]) if info["duration"] else "",
                         "thumbnail": info.get("thumbnail"), "uploader": info.get("uploader")})

    def _download(self, params):
        if not self._authorized(params):
            return self._error(401, "Senha de acesso necessária.")
        url = validate_url(params.get("url"))
        opts = parse_options(params)
        if not _slots.acquire(timeout=1):
            raise UserError("O servidor está ocupado com outros downloads. Tente de novo em instantes.", 429)
        workdir = tempfile.mkdtemp(prefix=tr.WORK_PREFIX + "clip-")
        try:
            path, filename, mime = make_clip(url, opts, workdir)
            size = os.path.getsize(path)
            ascii_name = filename.encode("ascii", "ignore").decode() or "audio." + FORMATS[opts["format"]]["ext"]
            ascii_name = ascii_name.replace('"', "")
            quoted = urllib.parse.quote(filename)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}")
            self.send_header("X-Filename", quoted)
            self.send_header("Access-Control-Expose-Headers", "X-Filename, Content-Disposition")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                with open(path, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, 256 * 1024)
        finally:
            _slots.release()
            shutil.rmtree(workdir, ignore_errors=True)

    def _save_cookies(self):
        if not ACCESS_PASSWORD:
            raise UserError("Para enviar cookies, defina a variável SENHA_ACESSO na stack.", 403)
        if not self._authorized():
            return self._error(401, "Senha de acesso necessária.")
        raw = self._body(limit=1_000_000).decode("utf-8", "replace")
        if "\t" not in raw or ("youtube.com" not in raw and "google.com" not in raw):
            raise UserError("Arquivo inválido: envie o cookies.txt no formato Netscape exportado do navegador.")
        if not raw.lstrip().startswith("# "):
            raw = "# Netscape HTTP Cookie File\n" + raw
        os.makedirs(os.path.dirname(COOKIES_FILE), exist_ok=True)
        tmp = COOKIES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(raw)
        os.chmod(tmp, 0o600)
        os.replace(tmp, COOKIES_FILE)
        with _info_lock:
            _info_cache.clear()
        self._json(200, {"ok": True})


def main():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError:
        log.warning("não consegui criar %s (cookies não serão salvos)", DATA_DIR)
    tr.startup_cleanup()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    log.info("YTB Conversor %s ouvindo na porta %d (senha: %s, trecho máx.: %ss)", VERSION, PORT,
             "sim" if ACCESS_PASSWORD else "não", MAX_CLIP_SECONDS)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
