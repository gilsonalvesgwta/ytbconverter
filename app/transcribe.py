"""
Transcrição de vídeo/áudio do YTB Conversor — em fluxo contínuo.

Fluxo (o mais rápido possível):
  1. O navegador extrai só o áudio do vídeo (MP4/MOV/M4A) e envia em partes;
     outros formatos sobem como estão.
  2. Assim que as primeiras partes chegam, o ffmpeg já decodifica o áudio
     para a memória (PCM 16 kHz mono) — nenhum WAV é gravado em disco.
  3. A transcrição acontece em blocos enquanto o resto ainda está chegando;
     o texto parcial aparece na tela.

Motores:
  - "local": faster-whisper na própria VPS (gratuito). O modelo fica
    carregado na memória para começar na hora.
  - "openai": API da OpenAI (pago; padrão gpt-4o-mini-transcribe, o mais
    barato), com até OPENAI_PARALLEL blocos ao mesmo tempo.
"""

import concurrent.futures
import json
import logging
import os
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("ytbconversor")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(tempfile.gettempdir(), "ytbc-uploads"))
UPLOAD_MAX_MB = int(os.environ.get("UPLOAD_MAX_MB", "4096"))
CHUNK_MAX_BYTES = 64 * 1024 * 1024
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
OPENAI_CHUNK_SECONDS = int(os.environ.get("OPENAI_CHUNK_SECONDS", "300"))
OPENAI_PARALLEL = max(1, int(os.environ.get("OPENAI_PARALLEL", "4")))
LOCAL_BLOCK_SECONDS = int(os.environ.get("LOCAL_BLOCK_SECONDS", "120"))
FIRST_BLOCK_SECONDS = int(os.environ.get("FIRST_BLOCK_SECONDS", "30"))
STREAM_IDLE_SECONDS = int(os.environ.get("STREAM_IDLE_SECONDS", "600"))  # envio parado no meio
PRELOAD_MODEL = os.environ.get("PRELOAD_MODEL", "true") == "true"
MODELS_DIR = os.path.join(DATA_DIR, "models")
JOB_TTL = 24 * 3600
UPLOAD_IDLE_SECONDS = int(os.environ.get("UPLOAD_IDLE_SECONDS", "3600"))   # envio abandonado
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "300"))
WORK_PREFIX = "ytbcw-"  # todo arquivo temporário do sistema começa assim
SR = 16000

LOCAL_MODELS = [
    ("tiny", "Tiny — o mais rápido, qualidade básica"),
    ("base", "Base — rápido, qualidade razoável"),
    ("small", "Small — equilíbrio (recomendado)"),
    ("medium", "Medium — melhor qualidade, mais lento"),
    ("large-v3-turbo", "Large v3 Turbo — máxima qualidade, bem mais lento"),
]
OPENAI_MODELS = [
    ("gpt-4o-mini-transcribe", "GPT-4o mini Transcribe — o mais barato (~US$ 0,003/min)"),
    ("gpt-transcribe", "GPT Transcribe — mais novo (~US$ 0,0045/min)"),
    ("whisper-1", "Whisper-1 (~US$ 0,006/min)"),
]
LANGUAGES = [("pt", "Português"), ("auto", "Detectar automaticamente"), ("en", "Inglês"), ("es", "Espanhol")]
DEFAULT_SETTINGS = {"engine": "local", "local_model": "small", "openai_model": "gpt-4o-mini-transcribe",
                    "openai_key": "", "language": "pt", "skip_silence": False}
# formatos que o ffmpeg só lê com o arquivo completo (índice pode estar no fim)
NEEDS_WHOLE_FILE = (".mp4", ".m4a", ".m4v", ".mov", ".3gp", ".3g2")
STREAM_FORMATS = {"adts": ["-f", "aac"], "mp3": ["-f", "mp3"], "file": []}
MUSIC_ONLY = re.compile(r"[\s♪♫♬♩🎵🎶*.\-]*|\[(música|music|musique|aplausos|applause)\]|\((música|music)\)", re.I)


class TranscribeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- configurações
_settings_lock = threading.Lock()


def load_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        data.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return data


def public_settings():
    s = load_settings()
    key = s.pop("openai_key")
    s["openai_key_set"] = bool(key)
    s["openai_key_hint"] = (key[:3] + "…" + key[-4:]) if len(key) > 10 else ""
    s["model_ready"] = _model_cache["name"] == s["local_model"]
    s["options"] = {"engines": [["local", "Gratuito — no próprio servidor"], ["openai", "OpenAI — pago, usa sua chave"]],
                    "local_models": LOCAL_MODELS, "openai_models": OPENAI_MODELS, "languages": LANGUAGES}
    return s


def update_settings(data, can_store_key):
    with _settings_lock:
        s = load_settings()
        choices = {"engine": ["local", "openai"], "local_model": [m for m, _ in LOCAL_MODELS],
                   "openai_model": [m for m, _ in OPENAI_MODELS], "language": [l for l, _ in LANGUAGES]}
        for field, allowed in choices.items():
            if field in data and data[field] is not None:
                if data[field] not in allowed:
                    raise TranscribeError(f"Valor inválido para {field}.")
                s[field] = data[field]
        if "skip_silence" in data:
            s["skip_silence"] = bool(data["skip_silence"])
        if data.get("clear_openai_key"):
            s["openai_key"] = ""
        key = str(data.get("openai_key") or "").strip()
        if key:
            if not can_store_key:
                raise TranscribeError("Para salvar a chave da OpenAI, defina a variável SENHA_ACESSO na stack.", 403)
            if not key.startswith("sk-") or len(key) < 20 or re.search(r"\s", key):
                raise TranscribeError("Chave da OpenAI inválida: ela começa com sk- (copie de platform.openai.com/api-keys).")
            s["openai_key"] = key
        if s["engine"] == "openai" and not s["openai_key"]:
            raise TranscribeError("Para usar a OpenAI, informe a chave da API.")
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SETTINGS_FILE)
    preload_model_async()
    return public_settings()


# ---------------------------------------------------------------- envio em partes
_uploads = {}
_uploads_lock = threading.Lock()


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _drop_upload(uid):
    u = _uploads.pop(uid, None)
    if u:
        _unlink(u["path"])


def _cleanup_uploads():
    now = time.time()
    with _uploads_lock:
        for uid in [u for u, v in _uploads.items() if not v.get("in_use") and now - v["touched"] > UPLOAD_IDLE_SECONDS]:
            log.info("apagando envio abandonado %s", uid[:8])
            _drop_upload(uid)


def startup_cleanup():
    """Ao iniciar, apaga sobras de execuções anteriores e pré-carrega o modelo."""
    removed = 0
    if os.path.isdir(UPLOAD_DIR):
        for name in os.listdir(UPLOAD_DIR):
            _unlink(os.path.join(UPLOAD_DIR, name))
            removed += 1
    tmp = tempfile.gettempdir()
    for name in os.listdir(tmp):
        if name.startswith(WORK_PREFIX):
            path = os.path.join(tmp, name)
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else _unlink(path)
            removed += 1
    if removed:
        log.info("limpeza ao iniciar: %d sobras temporárias apagadas", removed)
    threading.Thread(target=_janitor, name="limpeza", daemon=True).start()
    preload_model_async()


def _janitor():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            _cleanup_uploads()
        except Exception:  # noqa
            log.exception("falha na limpeza automática")


def upload_init(name, size):
    _cleanup_uploads()
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise TranscribeError("Tamanho do arquivo inválido.")
    if size <= 0:
        raise TranscribeError("O arquivo está vazio.")
    if size > UPLOAD_MAX_MB * 1024 * 1024:
        raise TranscribeError(f"Arquivo grande demais: o máximo é {UPLOAD_MAX_MB / 1024:.0f} GB.", 413)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    uid = uuid.uuid4().hex
    path = os.path.join(UPLOAD_DIR, uid)
    open(path, "wb").close()
    with _uploads_lock:
        _uploads[uid] = {"path": path, "size": size, "received": 0, "name": str(name or "arquivo")[:200],
                         "created": time.time(), "touched": time.time()}
    return {"upload_id": uid, "chunk_size": 50 * 1024 * 1024}


def upload_chunk(uid, offset, stream, length):
    with _uploads_lock:
        u = _uploads.get(uid)
    if not u:
        raise TranscribeError("Envio não encontrado (expirou?). Selecione o arquivo de novo.", 404)
    if length <= 0 or length > CHUNK_MAX_BYTES:
        raise TranscribeError("Parte do arquivo com tamanho inválido.")
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        raise TranscribeError("Posição inválida.")
    if offset > u["received"] or offset < 0:
        raise TranscribeError("Parte fora de ordem. Recomece o envio.", 409)
    if offset + length > u["size"]:
        raise TranscribeError("O arquivo enviado é maior que o informado.")
    if offset < u["received"] and u.get("streaming"):
        # a transcrição já está lendo: repetição de parte já recebida (nova tentativa) é ignorada,
        # só gravamos o que ainda não tinha chegado
        skip = u["received"] - offset
        if skip >= length:
            stream.read(length)
            return {"received": u["received"], "complete": u["received"] == u["size"]}
        stream.read(skip)
        offset, length = u["received"], length - skip
    with open(u["path"], "r+b") as fh:
        if not u.get("streaming"):
            fh.truncate(offset)  # reenvio da mesma parte (nova tentativa) sobrescreve
        fh.seek(offset)
        left = length
        while left > 0:
            buf = stream.read(min(1024 * 1024, left))
            if not buf:
                raise TranscribeError("Envio interrompido. Tente de novo.")
            fh.write(buf)
            left -= len(buf)
    u["received"] = offset + length
    u["touched"] = time.time()
    return {"received": u["received"], "complete": u["received"] == u["size"]}


# ---------------------------------------------------------------- tarefas
_jobs = {}
_jobs_lock = threading.Lock()
_queue = queue.Queue()
_worker_started = threading.Event()


def _start_worker():
    if not _worker_started.is_set():
        _worker_started.set()
        threading.Thread(target=_worker, name="transcricao", daemon=True).start()


def start_stream(name, size, language=None, fmt="file", duration=None):
    """Cria envio + transcrição de uma vez: a transcrição começa enquanto o envio acontece."""
    if fmt not in STREAM_FORMATS:
        raise TranscribeError("Formato de envio inválido.")
    up = upload_init(name, size)
    try:
        job = create_job({"upload_id": up["upload_id"], "stream": True, "fmt": fmt}, language, duration)
    except Exception:
        with _uploads_lock:
            _drop_upload(up["upload_id"])
        raise
    return {**up, "job": job}


def create_job(source, language=None, duration=None):
    """source: {"upload_id", ["stream", "fmt"]} ou {"url", "resolver": callable -> (url, headers, title, duração)}"""
    now = time.time()
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items() if now - v["created"] > JOB_TTL]:
            del _jobs[jid]
    settings = load_settings()
    lang = language or settings["language"]
    if lang not in [l for l, _ in LANGUAGES]:
        raise TranscribeError("Idioma inválido.")
    name, u = "link do YouTube", None
    if source.get("upload_id"):
        with _uploads_lock:
            u = _uploads.get(source["upload_id"])
        if not u:
            raise TranscribeError("Envio não encontrado (expirou?). Selecione o arquivo de novo.", 404)
        if not source.get("stream") and u["received"] != u["size"]:
            raise TranscribeError("O arquivo ainda não terminou de ser enviado.")
        name = u["name"]
    if settings["engine"] == "openai" and not settings["openai_key"]:
        raise TranscribeError("Configure a chave da OpenAI ou escolha o modo gratuito.")
    try:
        duration = max(0.0, float(duration or 0))
    except (TypeError, ValueError):
        duration = 0.0
    jid = uuid.uuid4().hex
    job = {"id": jid, "status": "queued", "stage": "Na fila", "progress": 0, "text": "", "error": "",
           "name": name, "language": lang, "engine": settings["engine"],
           "model": settings["local_model"] if settings["engine"] == "local" else settings["openai_model"],
           "duration": duration, "transcribed": 0, "received_audio": 0,
           "created": now, "started": None, "finished": None, "_source": source}
    if u is not None:
        u["in_use"] = True  # já está na fila: a limpeza automática não mexe
        u["streaming"] = bool(source.get("stream"))
    with _jobs_lock:
        _jobs[jid] = job
    _queue.put(jid)
    _start_worker()
    return get_job(jid)


def get_job(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
        if not job:
            return None
        out = {k: v for k, v in job.items() if not k.startswith("_")}
        if job["status"] == "queued":
            out["position"] = sum(1 for j in _jobs.values() if j["status"] == "queued" and j["created"] <= job["created"])
    return out


def _set(job, **kw):
    with _jobs_lock:
        job.update(kw)


def _worker():
    while True:
        jid = _queue.get()
        with _jobs_lock:
            job = _jobs.get(jid)
        if job:
            _run(job)


# ---------------------------------------------------------------- áudio em fluxo
class PcmStream:
    """Decodifica a origem com ffmpeg direto para a memória, enquanto ela chega."""

    def __init__(self, job, src):
        self.job, self.src = job, src
        self.buf = bytearray()
        self.base = 0          # amostra absoluta de buf[0]
        self.eof = False
        self.lock = threading.Lock()
        self.err = b""
        self.fail = None
        self.upload_id = src.get("upload_id")
        self.proc = None

    def start(self):
        src, cmd = self.src, ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        feed = None
        if self.upload_id:
            with _uploads_lock:
                u = _uploads.get(self.upload_id)
            if not u:
                raise TranscribeError("O arquivo enviado expirou. Envie de novo.")
            fmt = src.get("fmt", "file")
            whole = not src.get("stream") or (fmt == "file" and u["name"].lower().endswith(NEEDS_WHOLE_FILE))
            if whole:
                _set(self.job, stage="Recebendo o arquivo")
                self._wait_complete(u)
                cmd += ["-i", u["path"]]
            else:
                cmd += STREAM_FORMATS[fmt] + ["-i", "pipe:0"]
                feed = u
        else:
            url, headers, title, duration = src["resolver"]()
            _set(self.job, name=title)
            if duration and not self.job["duration"]:
                _set(self.job, duration=float(duration))
            if url.startswith("http"):
                hdrs = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
                if hdrs:
                    cmd += ["-headers", hdrs]
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            cmd += ["-i", url]
        cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        if feed:
            threading.Thread(target=self._feed, args=(feed,), daemon=True).start()
        elif self.upload_id:
            threading.Thread(target=self._drop_when_done, daemon=True).start()

    def _wait_complete(self, u):
        last, t = -1, time.time()
        while u["received"] < u["size"]:
            if u["received"] != last:
                last, t = u["received"], time.time()
            elif time.time() - t > STREAM_IDLE_SECONDS:
                raise TranscribeError("O envio do arquivo parou no meio. Tente de novo.")
            time.sleep(0.2)

    def _feed(self, u):
        """Entrega ao ffmpeg os bytes que já chegaram, conforme chegam."""
        pos, t = 0, time.time()
        try:
            with open(u["path"], "rb") as fh:
                while True:
                    avail = u["received"]
                    if avail > pos:
                        fh.seek(pos)
                        while pos < avail:
                            data = fh.read(min(1024 * 1024, avail - pos))
                            if not data:
                                break
                            self.proc.stdin.write(data)
                            pos += len(data)
                        t = time.time()
                    elif pos >= u["size"]:
                        break
                    elif time.time() - t > STREAM_IDLE_SECONDS:
                        self.fail = "O envio do arquivo parou no meio. Tente de novo."
                        self.proc.kill()
                        return
                    else:
                        time.sleep(0.1)
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            with _uploads_lock:
                _drop_upload(self.upload_id)  # já foi todo lido: libera o espaço na hora

    def _drop_when_done(self):
        self.proc.wait()
        with _uploads_lock:
            _drop_upload(self.upload_id)

    def _read_out(self):
        out = self.proc.stdout
        while True:
            data = out.read(64 * 1024)
            if not data:
                break
            with self.lock:
                self.buf += data
            _set(self.job, received_audio=round(self.available() / SR, 1))
        self.proc.wait()
        with self.lock:
            self.eof = True

    def _read_err(self):
        for line in self.proc.stderr:
            self.err = (self.err + line)[-3000:]

    def available(self):
        with self.lock:
            return self.base + len(self.buf) // 2

    def _slice(self, start, end, consume):
        import numpy as np
        with self.lock:
            a, b = (start - self.base) * 2, (end - self.base) * 2
            block = np.frombuffer(bytes(self.buf[a:b]), dtype=np.int16)
            if consume:
                del self.buf[:b]
                self.base = end
        return block

    def error_message(self):
        if self.fail:
            return self.fail
        err = self.err.decode("utf-8", "replace").lower()
        if "matches no streams" in err or "does not contain any stream" in err or "output file is empty" in err:
            return "Esse arquivo não tem faixa de áudio."
        if ("invalid data" in err or "could not find codec" in err or "moov atom not found" in err
                or "failed to read frame size" in err or "error opening input" in err or "invalid argument" in err):
            return "Não consegui ler esse arquivo: formato não reconhecido ou arquivo corrompido."
        return "Não consegui extrair o áudio: " + (err.strip()[-300:] or "arquivo sem áudio reconhecível.")

    def blocks(self, first, size):
        """Gera (início_em_segundos, amostras) à medida que o áudio chega.
        Cada corte cai no ponto mais silencioso dos últimos 15 s do bloco."""
        consumed, target, margin = 0, first, 15
        while True:
            avail = self.available()
            with self.lock:
                eof = self.eof
            if avail - consumed >= (target + margin) * SR:
                end = self._cut(consumed + target * SR, margin)
            elif eof and avail > consumed:
                end = avail
            elif eof:
                return
            else:
                time.sleep(0.15)
                continue
            yield consumed / SR, self._slice(consumed, end, True)
            consumed, target = end, size

    def _cut(self, ideal, margin):
        import numpy as np
        win = self._slice(ideal - margin * SR, ideal, False).astype(np.float32)
        frame = SR // 10
        n = len(win) // frame
        if n == 0:
            return ideal
        rms = np.sqrt((win[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
        best = n - 1 - int(np.argmin(rms[::-1]))  # mais silencioso; empate → mais perto do ideal
        return ideal - margin * SR + best * frame + frame // 2


def _run(job):
    settings = load_settings()
    engine = settings["engine"]
    _set(job, status="running", stage="Preparando o áudio", started=time.time(), engine=engine,
         model=settings["local_model"] if engine == "local" else settings["openai_model"])
    src = job["_source"]
    pcm = PcmStream(job, src)
    try:
        pcm.start()
        text = _openai(job, pcm, settings) if engine == "openai" else _local(job, pcm, settings)
        if pcm.fail:
            raise TranscribeError(pcm.fail)
        total = pcm.available() / SR
        if total < 0.3:
            raise TranscribeError(pcm.error_message())
        text = text.strip() or "(Nenhuma fala encontrada no áudio.)"
        _set(job, status="done", stage="Concluído", progress=100, text=text, duration=total, transcribed=total,
             finished=time.time())
        log.info("transcrição ok %s %.0fs de áudio em %.0fs (%s/%s)", job["id"][:8], total,
                 job["finished"] - job["started"], job["engine"], job["model"])
    except TranscribeError as exc:
        _set(job, status="error", stage="Erro", error=str(exc), finished=time.time())
    except Exception as exc:  # noqa
        log.exception("falha na transcrição")
        _set(job, status="error", stage="Erro", error="Erro inesperado na transcrição: " + str(exc)[:300],
             finished=time.time())
    finally:
        if pcm.proc and pcm.proc.poll() is None:
            pcm.proc.kill()
        if src.get("upload_id"):
            with _uploads_lock:
                _drop_upload(src["upload_id"])


def _progress(job, transcribed):
    dur = job["duration"] or 0
    pct = int(transcribed / dur * 100) if dur else 0
    _set(job, transcribed=round(transcribed, 1), progress=max(0, min(99, pct)))


# ---------------------------------------------------------------- motor gratuito
_model_cache = {"name": None, "model": None}
_model_lock = threading.Lock()


def _threads():
    env = int(os.environ.get("WHISPER_THREADS", "0") or 0)
    if env > 0:
        return env
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return os.cpu_count() or 2


def _get_model(name, job=None):
    from faster_whisper import WhisperModel
    with _model_lock:
        if _model_cache["name"] != name:
            if job:
                _set(job, stage="Carregando o modelo de transcrição (na 1ª vez ele é baixado, pode levar alguns minutos)")
            _model_cache.update(name=None, model=None)
            t0 = time.time()
            _model_cache["model"] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=_threads(),
                                                 download_root=MODELS_DIR)
            _model_cache["name"] = name
            log.info("modelo %s pronto em %.0fs", name, time.time() - t0)
        return _model_cache["model"]


def preload_model_async():
    """Deixa o modelo gratuito carregado na memória para a transcrição começar na hora."""
    if not PRELOAD_MODEL:
        return
    s = load_settings()
    if s["engine"] != "local" or _model_cache["name"] == s["local_model"]:
        return

    def run():
        try:
            import faster_whisper  # noqa
        except ImportError:
            return
        try:
            _get_model(s["local_model"])
        except Exception as exc:  # noqa
            log.warning("não consegui pré-carregar o modelo %s: %s", s["local_model"], exc)

    threading.Thread(target=run, name="pre-carga", daemon=True).start()


def _local(job, pcm, settings):
    try:
        import faster_whisper  # noqa
    except ImportError:
        raise TranscribeError("O motor gratuito não está instalado nesta imagem.", 500)
    import numpy as np
    model = _get_model(settings["local_model"], job)
    skip = bool(settings.get("skip_silence"))
    lang = None if job["language"] == "auto" else job["language"]
    beam = int(os.environ.get("WHISPER_BEAM", "1"))
    text, last_end, prompt = "", None, None
    _set(job, stage="Transcrevendo")
    for start, block in pcm.blocks(FIRST_BLOCK_SECONDS, LOCAL_BLOCK_SECONDS):
        audio = block.astype(np.float32) / 32768.0
        segments, _info = model.transcribe(
            audio, language=lang, beam_size=beam, vad_filter=skip,
            # sem isto o Whisper descarta janelas de 30 s que "parecem" não ter fala
            # (canto, fala com música de fundo) e a transcrição sai cortada
            no_speech_threshold=0.6 if skip else None,
            condition_on_previous_text=False, initial_prompt=prompt)
        for seg in segments:
            t = seg.text.strip()
            if MUSIC_ONLY.fullmatch(t):
                t = ""  # só símbolo de música: não é texto
            if t:
                s0 = start + seg.start
                if text:
                    text += "\n\n" if last_end is not None and s0 - last_end > 2.0 else " "
                text += t
                last_end = start + seg.end
                _set(job, text=text)
            _progress(job, start + seg.end)
        _progress(job, start + len(block) / SR)
        prompt = text[-200:] or None
    return text


# ---------------------------------------------------------------- motor OpenAI
def _openai(job, pcm, settings):
    key = settings["openai_key"]
    if not key:
        raise TranscribeError("Configure a chave da OpenAI em Configurações da transcrição.")
    results, done_secs, lock = {}, [0.0], threading.Lock()

    def work(idx, block):
        text = _openai_request(key, settings["openai_model"], _encode_mp3(block), job["language"], "").strip()
        with lock:
            results[idx] = text
            done_secs[0] += len(block) / SR
            ordered, i = [], 0
            while i in results:  # texto parcial sempre na ordem certa
                if results[i]:
                    ordered.append(results[i])
                i += 1
            _set(job, text=" ".join(ordered))
            _progress(job, done_secs[0])

    _set(job, stage="Transcrevendo")
    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=OPENAI_PARALLEL) as pool:
        for idx, (_start, block) in enumerate(pcm.blocks(OPENAI_CHUNK_SECONDS, OPENAI_CHUNK_SECONDS)):
            futures.append(pool.submit(work, idx, block))
            if any(f.done() and f.exception() for f in futures):
                break
        for fut in futures:
            fut.result()  # repassa erros (chave inválida etc.)
    return " ".join(results[i] for i in sorted(results) if results[i])


def _encode_mp3(block):
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1",
                           "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", "48k", "-f", "mp3", "pipe:1"],
                          input=block.tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise TranscribeError("Falha ao preparar o áudio: " + proc.stderr.decode("utf-8", "replace")[-200:])
    return proc.stdout


def _multipart(fields, file_field, data):
    boundary = "----ytbc" + secrets.token_hex(12)
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"audio.mp3\"\r\n"
            "Content-Type: audio/mpeg\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), boundary


def _openai_request(key, model, mp3, language, prompt):
    fields = {"model": model, "response_format": "json"}
    if language and language != "auto":
        fields["language"] = language
    if prompt:
        fields["prompt"] = prompt
    body, boundary = _multipart(fields, "file", mp3)
    url = OPENAI_BASE_URL + "/v1/audio/transcriptions"
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + key, "Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            with urllib.request.urlopen(req, timeout=600) as res:
                return json.loads(res.read().decode("utf-8")).get("text", "")
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", "replace")
            try:
                info = json.loads(raw).get("error") or {}
            except ValueError:
                info = {}
            msg, code = info.get("message") or raw[:200], info.get("code") or ""
            if err.code == 401:
                raise TranscribeError("A OpenAI recusou a chave (inválida ou revogada). Confira em Configurações.")
            if code == "insufficient_quota":
                raise TranscribeError("A conta da OpenAI está sem créditos. Adicione saldo em platform.openai.com/settings/organization/billing.")
            if err.code in (400, 404) and "model" in msg.lower():
                raise TranscribeError(f"O modelo {model} não está disponível para sua chave. Escolha outro em Configurações.")
            if err.code == 429 or err.code >= 500:
                last = f"OpenAI respondeu {err.code}: {msg}"
                time.sleep(3 * (attempt + 1))
                continue
            raise TranscribeError(f"A OpenAI recusou o pedido ({err.code}): {msg}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            last = f"Sem conexão com a OpenAI: {err}"
            time.sleep(3 * (attempt + 1))
    raise TranscribeError(last or "Falha ao falar com a OpenAI.")
