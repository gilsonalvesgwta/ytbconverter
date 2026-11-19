"""
Achar canais — descoberta de canais de música novos que estão crescendo rápido.

Usa a API oficial do YouTube (YouTube Data API v3, gratuita: 10.000 unidades/dia):
  1. search.list (100 unidades): vídeos de Música publicados no período, ordenados por
     visualizações, em várias regiões/gêneros (varredura);
  2. videos.list (1 unidade a cada 50): visualizações, duração, data;
  3. channels.list (1 unidade a cada 50): data de criação do canal, inscritos, totais.
Depois filtra pelos canais criados há pouco tempo e calcula métricas de crescimento.

Monetização: o YouTube não publica se um canal é monetizado. Damos duas pistas:
  - estimativa de elegibilidade pelas regras do Programa de Parcerias;
  - verificação de anúncios na página do vídeo (indício, sob demanda).
"""

import concurrent.futures
import datetime as dt
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("ytbconversor")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONF_FILE = os.path.join(DATA_DIR, "channels.json")
API_BASE = os.environ.get("YOUTUBE_API_BASE", "https://www.googleapis.com/youtube/v3").rstrip("/")
WEB_BASE = os.environ.get("YOUTUBE_WEB_BASE", "https://www.youtube.com").rstrip("/")
DAILY_QUOTA = int(os.environ.get("YOUTUBE_DAILY_QUOTA", "10000"))
MAX_SEARCHES = int(os.environ.get("CHANNELS_MAX_SEARCHES", "60"))
CACHE_TTL = int(os.environ.get("CHANNELS_CACHE_SECONDS", str(6 * 3600)))
COST = {"search": 100, "videos": 1, "channels": 1}

# varredura "mundo todo": regiões com muito consumo de música no YouTube
REGIONS = [
    ("US", "Estados Unidos"), ("BR", "Brasil"), ("MX", "México"), ("IN", "Índia"), ("ID", "Indonésia"),
    ("PH", "Filipinas"), ("GB", "Reino Unido"), ("DE", "Alemanha"), ("FR", "França"), ("ES", "Espanha"),
    ("IT", "Itália"), ("JP", "Japão"), ("KR", "Coreia do Sul"), ("VN", "Vietnã"), ("TH", "Tailândia"),
    ("TR", "Turquia"), ("NG", "Nigéria"), ("ZA", "África do Sul"), ("AR", "Argentina"), ("CO", "Colômbia"),
    ("CA", "Canadá"), ("AU", "Austrália"), ("PK", "Paquistão"), ("EG", "Egito"), ("SA", "Arábia Saudita"),
    ("PL", "Polônia"), ("NL", "Holanda"), ("RU", "Rússia"), ("UA", "Ucrânia"), ("PT", "Portugal"),
]
DEPTHS = {
    "rapida": {"label": "Rápida", "regions": [""], "pages": 2},
    "normal": {"label": "Normal", "regions": ["", "US", "BR", "MX", "IN", "ID", "PH", "DE"], "pages": 1},
    "profunda": {"label": "Profunda", "regions": [""] + [r for r, _ in REGIONS[:19]], "pages": 2},
}
DURATIONS = {"any": "Qualquer", "long": "Longos (+20 min, playlists)", "medium": "Médios (4–20 min)",
             "short": "Curtos (até 4 min)"}
LANGS = [("", "Qualquer idioma"), ("en", "Inglês"), ("pt", "Português"), ("es", "Espanhol"), ("hi", "Hindi"),
         ("id", "Indonésio"), ("fr", "Francês"), ("de", "Alemão"), ("ja", "Japonês"), ("ko", "Coreano"),
         ("ar", "Árabe"), ("tr", "Turco"), ("ru", "Russo"), ("it", "Italiano")]
GENRES = ["playlist", "lofi", "worship", "louvor", "gospel", "soft rock", "sad songs", "love songs", "sleep music",
          "relaxing music", "piano", "jazz", "country", "phonk", "reggaeton", "kpop", "rap", "christmas music",
          "80s", "ai music"]


class ChannelError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- configuração e cota
_lock = threading.Lock()


def _pt_today():
    """A cota do YouTube zera à meia-noite do horário do Pacífico."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:  # noqa
        return (dt.datetime.utcnow() - dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def _load():
    try:
        with open(CONF_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data.setdefault("api_key", "")
    data.setdefault("quota", {"date": _pt_today(), "used": 0})
    data.setdefault("favorites", [])
    if data["quota"].get("date") != _pt_today():
        data["quota"] = {"date": _pt_today(), "used": 0}
    return data


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF_FILE)


def _add_quota(units):
    with _lock:
        data = _load()
        data["quota"]["used"] += units
        _save(data)
        return data["quota"]["used"]


def public_settings():
    with _lock:
        d = _load()
    key = d["api_key"]
    return {"api_key_set": bool(key), "api_key_hint": (key[:4] + "…" + key[-4:]) if len(key) > 12 else "",
            "quota_used": d["quota"]["used"], "quota_limit": DAILY_QUOTA,
            "options": {"regions": REGIONS, "depths": {k: {"label": v["label"], "searches": len(v["regions"]) * v["pages"]}
                                                       for k, v in DEPTHS.items()},
                        "durations": DURATIONS, "langs": LANGS, "genres": GENRES}}


def update_settings(body, can_store_key):
    with _lock:
        d = _load()
        if body.get("clear_api_key"):
            d["api_key"] = ""
        key = str(body.get("api_key") or "").strip()
        if key:
            if not can_store_key:
                raise ChannelError("Para salvar a chave do YouTube, defina a variável SENHA_ACESSO na stack.", 403)
            if not re.fullmatch(r"[A-Za-z0-9_\-]{30,60}", key):
                raise ChannelError("Chave inválida: copie a chave da API (começa com AIza…) do Google Cloud.")
            d["api_key"] = key
        _save(d)
    return public_settings()


# ---------------------------------------------------------------- chamadas à API
_cache = {}
_cache_lock = threading.Lock()


def _api(endpoint, params, key, counter):
    """GET na API do YouTube com cache (economiza cota) e mensagens claras de erro."""
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    cache_key = endpoint + "?" + urllib.parse.urlencode(sorted(clean.items()))
    now = time.time()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < CACHE_TTL:
            counter["cached"] += 1
            return hit[1]
    url = f"{API_BASE}/{endpoint}?" + urllib.parse.urlencode({**clean, "key": key})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=30) as res:
                data = json.loads(res.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", "replace")
            try:
                e = json.loads(raw).get("error") or {}
            except ValueError:
                e = {}
            reasons = {x.get("reason") for x in e.get("errors") or []} | {
                (d.get("reason") or "") for d in e.get("details") or [] if isinstance(d, dict)}
            msg = e.get("message") or raw[:200]
            if "quotaExceeded" in reasons or "dailyLimitExceeded" in reasons:
                raise ChannelError("A cota diária gratuita da API do YouTube acabou. Ela renova à meia-noite "
                                   "(horário do Pacífico, por volta das 5h no Brasil).", 429)
            if "keyInvalid" in reasons or "API_KEY_INVALID" in reasons or "API key not valid" in msg:
                raise ChannelError("A chave da API do YouTube é inválida. Confira em Configurações da busca.", 400)
            if "accessNotConfigured" in reasons or "SERVICE_DISABLED" in reasons or "has not been used" in msg:
                raise ChannelError("A \"YouTube Data API v3\" não está ativada no seu projeto do Google Cloud. "
                                   "Ative em console.cloud.google.com → APIs e serviços → Biblioteca.", 400)
            if "ipRefererBlocked" in reasons or "API_KEY_IP_ADDRESS_BLOCKED" in reasons or "forbidden" in reasons:
                raise ChannelError("A chave tem restrição que bloqueia este servidor. No Google Cloud, deixe a chave "
                                   "restrita só à \"YouTube Data API v3\" (sem restrição de site/IP).", 400)
            if err.code >= 500:
                last = f"YouTube respondeu {err.code}: {msg}"
                time.sleep(1.5 * (attempt + 1))
                continue
            raise ChannelError(f"A API do YouTube recusou o pedido ({err.code}): {msg}", 502)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            last = f"Sem conexão com a API do YouTube: {err}"
            time.sleep(1.5 * (attempt + 1))
    else:
        raise ChannelError(last or "Falha na API do YouTube.", 502)
    cost = COST.get(endpoint, 1)
    with counter["lock"]:
        counter["units"] += cost
        counter["calls"] += 1
    _add_quota(cost)
    with _cache_lock:
        if len(_cache) > 3000:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:1000]:
                del _cache[k]
        _cache[cache_key] = (now, data)
    return data


# ---------------------------------------------------------------- utilidades
def parse_iso(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def parse_duration(iso):
    """PT1H2M3S → segundos (P1DT… também)."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _thumb(snippet, prefer=("medium", "high", "default")):
    th = (snippet or {}).get("thumbnails") or {}
    for k in prefer:
        if th.get(k, {}).get("url"):
            return th[k]["url"]
    return ""


def monetization_estimate(subs, total_views, avg_duration):
    """Estimativa de elegibilidade ao Programa de Parcerias (YPP).
    Regra: 1.000 inscritos + 4.000 h assistidas (12 meses) OU 1.000 inscritos + 10 mi de views de Shorts (90 dias).
    Horas estimadas = views × duração média × 30% de retenção (média de mercado para música)."""
    hours = (total_views or 0) * (avg_duration or 0) * 0.30 / 3600
    shorts = 0 < (avg_duration or 0) <= 60
    meets_watch = (total_views or 0) >= 10_000_000 if shorts else hours >= 4000
    if subs is None:
        status = "talvez" if meets_watch else "desconhecido"
    elif subs >= 1000 and meets_watch:
        status = "provavel"
    elif subs >= 500 and (hours >= 2000 or (shorts and (total_views or 0) >= 3_000_000)):
        status = "perto"
    else:
        status = "improvavel"
    return {"status": status, "watch_hours": int(hours), "shorts": shorts}


# ---------------------------------------------------------------- busca
def plan_searches(p):
    keywords = [k.strip() for k in (p.get("keywords") or []) if k and k.strip()][:10] or [""]
    if p.get("region"):
        regions, pages = [p["region"]], int(p.get("pages") or 2)
    else:
        d = DEPTHS.get(p.get("depth") or "normal", DEPTHS["normal"])
        regions, pages = d["regions"], d["pages"]
    plan = [(r, k) for k in keywords for r in regions]
    return plan, max(1, min(pages, 5))


def estimate_units(p):
    plan, pages = plan_searches(p)
    n = min(len(plan) * pages, MAX_SEARCHES)
    return {"searches": n, "units": n * 100 + 10}


def search_channels(p, key=None):
    key = key or _load()["api_key"]
    if not key:
        raise ChannelError("Configure a chave da API do YouTube em Configurações da busca (é gratuita).", 400)
    now = dt.datetime.now(dt.timezone.utc)
    video_days = max(1, min(int(p.get("video_days") or 30), 365))
    channel_days = max(1, min(int(p.get("channel_days") or 30), 3650))
    min_views = max(0, int(p.get("min_views") or 0))
    min_subs = _int(p.get("min_subs")) or 0
    duration = p.get("duration") if p.get("duration") in DURATIONS else "any"
    lang = p.get("lang") or ""
    after = (now - dt.timedelta(days=video_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan, pages = plan_searches(p)
    est = min(len(plan) * pages, MAX_SEARCHES) * 100
    with _lock:
        used = _load()["quota"]["used"]
    if used + est > DAILY_QUOTA and not p.get("force"):
        raise ChannelError(f"Esta busca usa cerca de {est} unidades e restam {max(0, DAILY_QUOTA - used)} hoje. "
                           "Escolha uma varredura menor ou menos gêneros.", 429)
    counter = {"units": 0, "calls": 0, "cached": 0, "lock": threading.Lock()}
    t0 = time.time()

    # 1) vídeos de Música mais vistos no período, em cada região/gênero
    def run_search(region, q):
        ids, token = [], None
        for _ in range(pages):
            data = _api("search", {"part": "snippet", "type": "video", "videoCategoryId": "10", "order": "viewCount",
                                   "publishedAfter": after, "maxResults": 50, "regionCode": region, "q": q,
                                   "relevanceLanguage": lang, "videoDuration": None if duration == "any" else duration,
                                   "safeSearch": "none", "pageToken": token}, key, counter)
            ids += [it["id"]["videoId"] for it in data.get("items") or [] if it.get("id", {}).get("videoId")]
            token = data.get("nextPageToken")
            if not token:
                break
        return ids

    plan = plan[: max(1, MAX_SEARCHES // pages)]
    video_ids, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(run_search, r, q) for r, q in plan]
        for f in futs:
            try:
                video_ids += f.result()
            except ChannelError as exc:
                if exc.status in (400, 429) and not video_ids:
                    raise
                errors.append(str(exc))
    video_ids = list(dict.fromkeys(video_ids))

    # 2) detalhes dos vídeos
    videos = {}

    def get_videos(batch):
        data = _api("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch), "maxResults": 50},
                    key, counter)
        return data.get("items") or []

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for items in pool.map(get_videos, [video_ids[i:i + 50] for i in range(0, len(video_ids), 50)]):
            for v in items:
                sn, st = v.get("snippet") or {}, v.get("statistics") or {}
                pub = parse_iso(sn.get("publishedAt"))
                videos[v["id"]] = {
                    "id": v["id"], "title": sn.get("title") or "", "channel_id": sn.get("channelId"),
                    "published": sn.get("publishedAt"), "views": _int(st.get("viewCount")) or 0,
                    "likes": _int(st.get("likeCount")), "comments": _int(st.get("commentCount")),
                    "duration": parse_duration((v.get("contentDetails") or {}).get("duration")),
                    "thumb": _thumb(sn), "age_hours": max(1.0, (now - pub).total_seconds() / 3600) if pub else None,
                    "language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "",
                }
    for v in videos.values():
        v["views_per_hour"] = int(v["views"] / v["age_hours"]) if v["age_hours"] else 0

    # 3) canais dos vídeos com views suficientes
    by_channel = {}
    for v in videos.values():
        if v["views"] >= min_views and v["channel_id"]:
            by_channel.setdefault(v["channel_id"], []).append(v)
    ch_ids = list(by_channel)

    def get_channels(batch):
        data = _api("channels", {"part": "snippet,statistics", "id": ",".join(batch), "maxResults": 50}, key, counter)
        return data.get("items") or []

    channels = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for items in pool.map(get_channels, [ch_ids[i:i + 50] for i in range(0, len(ch_ids), 50)]):
            for c in items:
                sn, st = c.get("snippet") or {}, c.get("statistics") or {}
                created = parse_iso(sn.get("publishedAt"))
                if not created:
                    continue
                age_days = max(0.04, (now - created).total_seconds() / 86400)
                if age_days > channel_days:
                    continue
                subs = None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount"))
                if min_subs and (subs or 0) < min_subs:
                    continue
                vids = sorted(by_channel.get(c["id"], []), key=lambda v: -v["views"])
                total_views = _int(st.get("viewCount")) or sum(v["views"] for v in vids)
                avg_dur = sum(v["duration"] for v in vids) / len(vids) if vids else 0
                top = vids[0] if vids else None
                channels.append({
                    "id": c["id"], "title": sn.get("title") or "", "handle": sn.get("customUrl") or "",
                    "url": f"https://www.youtube.com/channel/{c['id']}", "thumb": _thumb(sn, ("default", "medium", "high")),
                    "country": sn.get("country") or "", "created": sn.get("publishedAt"), "age_days": round(age_days, 1),
                    "subs": subs, "views": total_views, "video_count": _int(st.get("videoCount")) or len(vids),
                    "views_per_day": int(total_views / max(1.0, age_days)),
                    "subs_per_day": int(subs / max(1.0, age_days)) if subs is not None else None,
                    "top_views": top["views"] if top else 0,
                    "top_views_per_hour": top["views_per_hour"] if top else 0,
                    "avg_duration": int(avg_dur), "videos": vids[:5],
                    "monetization": monetization_estimate(subs, total_views, avg_dur),
                })
    # pontuação: velocidade de views ponderada pela novidade do canal
    for ch in channels:
        ch["score"] = round(math.log10(ch["views_per_day"] + 1) * 20 + math.log10(ch["top_views_per_hour"] + 1) * 10
                            + max(0.0, 30 - ch["age_days"]) * 0.5, 1)
    channels.sort(key=lambda c: -c["score"])
    with _lock:
        used = _load()["quota"]["used"]
    return {"channels": channels,
            "stats": {"searches": len(plan) * pages, "videos": len(videos), "channels_checked": len(ch_ids),
                      "found": len(channels), "units": counter["units"], "cached_calls": counter["cached"],
                      "seconds": round(time.time() - t0, 1), "errors": errors[:3]},
            "quota": {"used": used, "limit": DAILY_QUOTA}}


# ---------------------------------------------------------------- verificação de anúncios (indício)
def ad_check(video_id, cookie_header=""):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{11}", video_id or ""):
        raise ChannelError("Vídeo inválido.")
    url = f"{WEB_BASE}/watch?v={video_id}&hl=en&gl=US&has_verified=1&bpctr=9999999999"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/131.0 Safari/537.36",
               "Accept-Language": "en-US,en;q=0.9", "Cookie": cookie_header or "CONSENT=YES+1; SOCS=CAI"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as res:
            html = res.read(3_000_000).decode("utf-8", "replace")
    except Exception as exc:  # noqa
        return {"status": "erro", "detail": f"Não consegui abrir o vídeo: {exc}"[:200]}
    if "ytInitialPlayerResponse" not in html:
        return {"status": "erro", "detail": "O YouTube não entregou a página do vídeo (verificação de robô ou consentimento)."}
    if re.search(r'"(adPlacements|playerAds|adSlots)"\s*:\s*\[\s*\{', html):
        return {"status": "anuncios", "detail": "Anúncios encontrados no vídeo: forte indício de canal monetizado."}
    return {"status": "sem_anuncios", "detail": "Nenhum anúncio nesta visualização (pode variar por país/usuário)."}


# ---------------------------------------------------------------- favoritos
FAV_FIELDS = ("id", "title", "handle", "url", "thumb", "country", "created", "subs", "views", "video_count",
              "views_per_day", "monetization")


def list_favorites():
    with _lock:
        return _load()["favorites"]


def add_favorite(ch):
    if not isinstance(ch, dict) or not re.fullmatch(r"UC[A-Za-z0-9_\-]{20,24}", str(ch.get("id") or "")):
        raise ChannelError("Canal inválido.")
    item = {k: ch.get(k) for k in FAV_FIELDS}
    item["saved_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item["note"] = str(ch.get("note") or "")[:500]
    with _lock:
        d = _load()
        d["favorites"] = [f for f in d["favorites"] if f.get("id") != item["id"]] + [item]
        d["favorites"] = d["favorites"][-500:]
        _save(d)
        return d["favorites"]


def remove_favorite(cid):
    with _lock:
        d = _load()
        d["favorites"] = [f for f in d["favorites"] if f.get("id") != cid]
        _save(d)
        return d["favorites"]
