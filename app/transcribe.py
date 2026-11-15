"""
Transcrição de vídeo/áudio do YTB Conversor.

Motores:
  - "local": faster-whisper rodando na própria VPS (gratuito, sem chave).
  - "openai": API da OpenAI (pago; padrão gpt-4o-mini-transcribe, o mais barato).

Arquivos grandes chegam em partes (o proxy do Cloudflare limita cada envio
a 100 MB) e cada transcrição vira uma tarefa numa fila, consultada pela
interface (assim nenhuma requisição demora mais que alguns segundos).
"""

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
MODELS_DIR = os.path.join(DATA_DIR, "models")
JOB_TTL = 24 * 3600
UPLOAD_IDLE_SECONDS = int(os.environ.get("UPLOAD_IDLE_SECONDS", "3600"))   # envio abandonado
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "300"))
WORK_PREFIX = "ytbcw-"  # toda pasta temporária do sistema começa assim

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
    return public_settings()


# ---------------------------------------------------------------- envio em partes
_uploads = {}
_uploads_lock = threading.Lock()


def _cleanup_uploads():
    now = time.time()
    with _uploads_lock:
        for uid in [u for u, v in _uploads.items() if not v.get("in_use") and now - v["touched"] > UPLOAD_IDLE_SECONDS]:
            log.info("apagando envio abandonado %s", uid[:8])
            _drop_upload(uid)


def startup_cleanup():
    """Ao iniciar, apaga sobras de execuções anteriores (envios e pastas de trabalho)."""
    removed = 0
    if os.path.isdir(UPLOAD_DIR):
        for name in os.listdir(UPLOAD_DIR):
            try:
                os.unlink(os.path.join(UPLOAD_DIR, name))
                removed += 1
            except OSError:
                pass
    tmp = tempfile.gettempdir()
    for name in os.listdir(tmp):
        if name.startswith(WORK_PREFIX):
            path = os.path.join(tmp, name)
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else _unlink(path)
            removed += 1
    if removed:
        log.info("limpeza ao iniciar: %d sobras temporárias apagadas", removed)
    threading.Thread(target=_janitor, name="limpeza", daemon=True).start()


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _janitor():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            _cleanup_uploads()
        except Exception:  # noqa
            log.exception("falha na limpeza automática")


def _drop_upload(uid):
    u = _uploads.pop(uid, None)
    if u:
        try:
            os.unlink(u["path"])
        except OSError:
            pass


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
    with open(u["path"], "r+b") as fh:
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


def create_job(source, language=None):
    """source: {"upload_id": ...} ou {"url": ..., "resolver": callable -> (stream_url, headers, title)}"""
    now = time.time()
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items() if now - v["created"] > JOB_TTL]:
            del _jobs[jid]
    settings = load_settings()
    lang = language or settings["language"]
    if lang not in [l for l, _ in LANGUAGES]:
        raise TranscribeError("Idioma inválido.")
    name = "link do YouTube"
    if source.get("upload_id"):
        with _uploads_lock:
            u = _uploads.get(source["upload_id"])
        if not u:
            raise TranscribeError("Envio não encontrado (expirou?). Selecione o arquivo de novo.", 404)
        if u["received"] != u["size"]:
            raise TranscribeError("O arquivo ainda não terminou de ser enviado.")
        name = u["name"]
    if settings["engine"] == "openai" and not settings["openai_key"]:
        raise TranscribeError("Configure a chave da OpenAI ou escolha o modo gratuito.")
    jid = uuid.uuid4().hex
    job = {"id": jid, "status": "queued", "stage": "Na fila", "progress": 0, "text": "", "error": "",
           "name": name, "language": lang, "engine": settings["engine"],
           "model": settings["local_model"] if settings["engine"] == "local" else settings["openai_model"],
           "duration": 0, "created": now, "started": None, "finished": None, "_source": source}
    if source.get("upload_id"):
        u["in_use"] = True  # já está na fila: a limpeza automática não mexe
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


def _run(job):
    settings = load_settings()
    _set(job, status="running", stage="Preparando o áudio", started=time.time(),
         engine=settings["engine"], model=settings["local_model"] if settings["engine"] == "local" else settings["openai_model"])
    workdir = tempfile.mkdtemp(prefix=WORK_PREFIX + "tr-")
    src = job["_source"]
    try:
        headers = {}
        if src.get("upload_id"):
            with _uploads_lock:
                u = _uploads.get(src["upload_id"])
            if not u:
                raise TranscribeError("O arquivo enviado expirou. Envie de novo.")
            input_path = u["path"]
        else:
            input_path, headers, title = src["resolver"]()
            _set(job, name=title)
        wav = os.path.join(workdir, "audio.wav")
        _to_wav(input_path, headers, wav)
        if src.get("upload_id"):
            # o arquivo original não é mais necessário: libera o espaço já
            with _uploads_lock:
                _drop_upload(src["upload_id"])
        duration = _duration(wav)
        if duration < 0.3:
            raise TranscribeError("Não encontrei áudio nesse arquivo.")
        _set(job, duration=duration)
        if settings["engine"] == "openai":
            text = _openai(job, wav, workdir, settings)
        else:
            text = _local(job, wav, settings)
        text = text.strip()
        if not text:
            text = "(Nenhuma fala encontrada no áudio.)"
        _set(job, status="done", stage="Concluído", progress=100, text=text, finished=time.time())
        log.info("transcrição ok %s %.0fs de áudio em %.0fs (%s/%s)", job["id"][:8], duration,
                 job["finished"] - job["started"], job["engine"], job["model"])
    except TranscribeError as exc:
        _set(job, status="error", stage="Erro", error=str(exc), finished=time.time())
    except Exception as exc:  # noqa
        log.exception("falha na transcrição")
        _set(job, status="error", stage="Erro", error="Erro inesperado na transcrição: " + str(exc)[:300],
             finished=time.time())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if src.get("upload_id"):
            with _uploads_lock:
                _drop_upload(src["upload_id"])


# ---------------------------------------------------------------- áudio
def _to_wav(src, headers, out):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if src.startswith("http"):
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        if hdrs:
            cmd += ["-headers", hdrs]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    cmd += ["-i", src, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        err = proc.stderr.lower()
        if "matches no streams" in err or "does not contain any stream" in err:
            raise TranscribeError("Esse arquivo não tem faixa de áudio.")
        if "invalid data" in err or "could not find codec" in err or "moov atom not found" in err:
            raise TranscribeError("Não consegui ler esse arquivo: formato não reconhecido ou arquivo corrompido.")
        raise TranscribeError("Não consegui extrair o áudio: " + proc.stderr.strip()[-300:])


def _duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


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


def _local(job, wav, settings):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise TranscribeError("O motor gratuito não está instalado nesta imagem.", 500)
    name = settings["local_model"]
    with _model_lock:
        if _model_cache["name"] != name:
            _set(job, stage="Carregando o modelo de transcrição (na 1ª vez ele é baixado, pode levar alguns minutos)")
            _model_cache["model"] = None
            _model_cache["model"] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=_threads(),
                                                 download_root=MODELS_DIR)
            _model_cache["name"] = name
        model = _model_cache["model"]
    _set(job, stage="Transcrevendo")
    skip = bool(settings.get("skip_silence"))
    lang = None if job["language"] == "auto" else job["language"]
    # áudio já está em WAV 16 kHz mono: entregamos as amostras prontas
    # (evita a leitura via PyAV, que muda entre versões)
    import numpy as np
    import wave
    with wave.open(wav, "rb") as fh:
        audio = np.frombuffer(fh.readframes(fh.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    segments, _info = model.transcribe(audio, language=lang, beam_size=int(os.environ.get("WHISPER_BEAM", "1")),
                                       vad_filter=skip,
                                       # sem isto o Whisper descarta janelas de 30 s que "parecem" não ter
                                       # fala (canto, fala com música de fundo) e a transcrição sai cortada
                                       no_speech_threshold=0.6 if skip else None,
                                       condition_on_previous_text=False)
    duration = job["duration"] or 1
    parts, last_end = [], None
    for seg in segments:
        text = seg.text.strip()
        if re.fullmatch(r"[\s♪♫♬♩🎵🎶*.\-]*|\[(música|music|musique|aplausos|applause)\]|\((música|music)\)", text, re.I):
            text = ""  # só símbolo de música: não é texto
        if text:
            if last_end is not None:
                parts.append("\n\n" if seg.start - last_end > 2.0 else " ")
            parts.append(text)
            last_end = seg.end
        _set(job, progress=min(99, int(seg.end / duration * 100)))
    return "".join(parts)


# ---------------------------------------------------------------- motor OpenAI
def _split_ranges(wav, duration, target):
    """Divide em partes de ~target segundos, cortando em silêncios quando possível."""
    if duration <= target * 1.1:
        return [(0.0, duration)]
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", wav, "-af",
                           "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-"], capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    mids = [(a + b) / 2 for a, b in zip(starts, ends)]
    cuts, pos = [], 0.0
    while duration - pos > target * 1.1:
        ideal = pos + target
        near = [m for m in mids if ideal - 30 <= m <= ideal + 15 and m > pos + 10]
        cut = min(near, key=lambda m: abs(m - ideal)) if near else ideal
        cuts.append(cut)
        pos = cut
    edges = [0.0] + cuts + [duration]
    return list(zip(edges[:-1], edges[1:]))


def _openai(job, wav, workdir, settings):
    key = settings["openai_key"]
    if not key:
        raise TranscribeError("Configure a chave da OpenAI em Configurações da transcrição.")
    ranges = _split_ranges(wav, job["duration"], OPENAI_CHUNK_SECONDS)
    texts, prompt = [], ""
    for i, (a, b) in enumerate(ranges):
        _set(job, stage=f"Transcrevendo parte {i + 1} de {len(ranges)}" if len(ranges) > 1 else "Transcrevendo",
             progress=int(i / len(ranges) * 100))
        part = os.path.join(workdir, f"parte{i}.mp3")
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{a:.3f}",
                               "-t", f"{b - a:.3f}", "-i", wav, "-c:a", "libmp3lame", "-b:a", "48k", part],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise TranscribeError("Falha ao preparar o áudio: " + proc.stderr[-200:])
        text = _openai_request(key, settings["openai_model"], part, job["language"], prompt).strip()
        if text:
            texts.append(text)
            prompt = text[-400:]
        os.unlink(part)
    return " ".join(texts)


def _multipart(fields, file_field, file_path):
    boundary = "----ytbc" + secrets.token_hex(12)
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    with open(file_path, "rb") as fh:
        data = fh.read()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"audio.mp3\"\r\n"
            "Content-Type: audio/mpeg\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), boundary


def _openai_request(key, model, path, language, prompt):
    fields = {"model": model, "response_format": "json"}
    if language and language != "auto":
        fields["language"] = language
    if prompt:
        fields["prompt"] = prompt
    body, boundary = _multipart(fields, "file", path)
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
