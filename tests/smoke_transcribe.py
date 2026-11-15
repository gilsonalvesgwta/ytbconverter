"""Teste rápido de transcrição gratuita contra um servidor já rodando.
Uso: python tests/smoke_transcribe.py http://localhost:8080 SENHA"""
import json, os, subprocess, sys, tempfile, time, urllib.request

base, key = sys.argv[1].rstrip("/"), sys.argv[2]
H = {"X-Access-Key": key}


def call(path, data=None, method=None, raw=None):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    hdr = dict(H, **({"Content-Type": "application/json"} if data is not None else {}))
    with urllib.request.urlopen(urllib.request.Request(base + path, data=body, method=method, headers=hdr), timeout=60) as r:
        return json.loads(r.read())


tmp = tempfile.mkdtemp()
wav = os.path.join(tmp, "fala.wav")
subprocess.run(["espeak-ng", "-v", "pt-br", "-s", "140", "-w", wav,
                "Bom dia a todos. Hoje vamos falar sobre consórcio e planejamento financeiro para comprar a casa própria."], check=True)
call("/api/transcribe/settings", {"engine": "local", "local_model": "base"})
data = open(wav, "rb").read()
up = call("/api/upload/init", {"name": "fala.wav", "size": len(data)})
call(f"/api/upload/{up['upload_id']}?offset=0", raw=data, method="PUT")
job = call("/api/transcribe", {"upload_id": up["upload_id"]})["job"]
t0 = time.time()
while job["status"] not in ("done", "error") and time.time() - t0 < 900:
    time.sleep(2)
    job = call("/api/jobs/" + job["id"])["job"]
    print(f"{int(time.time() - t0)}s {job['status']} {job['stage']} {job['progress']}%")
print("TRANSCRICAO:", job.get("text") or job.get("error"))
text = (job.get("text") or "").lower()
hits = [w for w in ["bom dia", "todos", "consórcio", "planejamento", "financeiro", "casa"] if w in text]
sys.exit(0 if job["status"] == "done" and len(hits) >= 3 else 1)
