"""Compara velocidade e texto: modo simples (um trecho por vez) x lote (vários de uma vez).
Uso: python tests/bench_whisper.py [modelo]   (precisa de faster-whisper e espeak-ng)"""
import difflib, os, subprocess, sys, tempfile, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import transcribe as tr
from faster_whisper import WhisperModel, BatchedInferencePipeline

model_name = sys.argv[1] if len(sys.argv) > 1 else "base"
frases = ["Bom dia a todos, hoje vamos falar sobre planejamento financeiro.",
          "O consórcio é uma forma de comprar a casa própria sem pagar juros.",
          "Muitas famílias brasileiras usam o crédito para construir patrimônio.",
          "É importante organizar o orçamento antes de assumir qualquer compromisso.",
          "Vamos ver agora alguns exemplos práticos com números reais.",
          "No final da aula você vai entender como funciona a carta de crédito."]
tmp = tempfile.mkdtemp()
parts = []
for i in range(24):  # ~4 min de fala
    w = os.path.join(tmp, f"f{i}.wav")
    subprocess.run(["espeak-ng", "-v", "pt-br", "-s", "150", "-w", w, frases[i % len(frases)]], check=True)
    parts.append(w)
lst = os.path.join(tmp, "l.txt")
open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-ac", "1", "-ar", "16000",
                      "-f", "s16le", "-"], capture_output=True, check=True).stdout
audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
dur = len(audio) / 16000
model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=tr._threads(), download_root="/tmp/models")
common = dict(language="pt", beam_size=1, no_speech_threshold=None, condition_on_previous_text=False)


def run(fn):
    t0 = time.time()
    segs, _ = fn()
    text = " ".join(s.text.strip() for s in segs)
    return time.time() - t0, text


fn_simple = lambda: model.transcribe(audio, vad_filter=False, **common)
pipe = BatchedInferencePipeline(model=model)
fn_batch = lambda: pipe.transcribe(audio, batch_size=8, vad_filter=False, clip_timestamps=tr.plan_clips(audio), **common)
run(fn_simple)  # aquece
t1, txt1 = run(fn_simple)
t2, txt2 = run(fn_batch)
ref = " ".join(frases[i % len(frases)] for i in range(24)).lower().split()
score = lambda t: difflib.SequenceMatcher(None, ref, t.lower().split()).ratio()
print(f"ÁUDIO {dur:.0f}s | modelo {model_name} | {tr._threads()} núcleos")
print(f"SIMPLES: {t1:.1f}s ({dur / t1:.1f}x tempo real) | acerto {score(txt1):.0%}")
print(f"LOTE:    {t2:.1f}s ({dur / t2:.1f}x tempo real) | acerto {score(txt2):.0%} | {t1 / t2:.2f}x mais rápido")
print("LOTE TEXTO:", txt2[:300])
