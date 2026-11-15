"""Compara variantes do modo em lote (velocidade). Uso: python tests/bench_variants.py"""
import os, subprocess, sys, tempfile, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import transcribe as tr
from faster_whisper import WhisperModel, BatchedInferencePipeline

frases = ["Good morning everyone, today we will talk about financial planning.",
          "Every sunrise asks where you have gone, every night still waits for your call.",
          "I hold the words I never said, like fading stars I cannot reach.",
          "Come back home before the daylight leaves my eyes."]
tmp = tempfile.mkdtemp(); parts = []
for i in range(48):
    w = os.path.join(tmp, f"f{i}.wav")
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", w, frases[i % len(frases)]], check=True); parts.append(w)
lst = os.path.join(tmp, "l.txt"); open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                     capture_output=True, check=True).stdout
audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
dur = len(audio) / 16000
for name in ["base", "tiny", "small"]:
    model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=tr._threads(), download_root="/tmp/models")
    pipe = BatchedInferencePipeline(model=model)
    clips = tr.plan_clips(audio)
    variants = {"lote8 + tempo por palavra (atual)": dict(batch_size=8, word_timestamps=True),
                "lote8 + tempo do modelo": dict(batch_size=8, word_timestamps=False, without_timestamps=False),
                "lote16 + tempo do modelo": dict(batch_size=16, word_timestamps=False, without_timestamps=False),
                "lote8 sem tempo": dict(batch_size=8, word_timestamps=False)}
    if name != "base":
        variants = {k: v for k, v in variants.items() if "lote8" in k and "sem" not in k}
    list(pipe.transcribe(audio[: 16000 * 30], language="en", beam_size=1, vad_filter=False, clip_timestamps=[{"start": 0, "end": 30}])[0])
    for label, kw in variants.items():
        t0 = time.time()
        segs = list(pipe.transcribe(audio, language="en", beam_size=1, vad_filter=False, clip_timestamps=clips,
                                    no_speech_threshold=None, condition_on_previous_text=False, **kw)[0])
        t = time.time() - t0
        print(f"VARIANTE {name:5s} | {label:36s} | {t:5.1f}s | {dur / t:5.1f}x tempo real | {len(segs)} trechos | ex: {segs[1].start:.1f}-{segs[1].end:.1f} {segs[1].text.strip()[:50]!r}")
