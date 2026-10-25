#!/usr/bin/env python3
"""Pipeline: cenas.json -> imagens (OpenAI) -> narração (OpenAI TTS) -> legendas -> vídeo 16:9 (ffmpeg).

Uso:
  export OPENAI_API_KEY=...
  python3 gerar.py imagens [--so 1,2,3]   # gera as imagens das cenas (cache em saida/img)
  python3 gerar.py audio                   # gera a narração por cena (cache em saida/audio)
  python3 gerar.py montar                  # clipes + legendas + vídeo final
  python3 gerar.py tudo
"""
import argparse, base64, json, re, subprocess, sys
from pathlib import Path

RAIZ = Path(__file__).parent
OUT = RAIZ / "saida"
IMG, AUD, CLIP = OUT / "img", OUT / "audio", OUT / "clips"
FPS, LARG, ALT = 30, 1920, 1080
PAUSA = 0.45  # silêncio no fim de cada cena (s)

MODELO_IMG = "gpt-image-2"
QUALIDADE_IMG = "medium"  # low | medium | high
MODELO_TTS, VOZ = "gpt-4o-mini-tts", "onyx"
INSTR_TTS = ("Narre em português do Brasil, com voz masculina grave e envolvente, como um "
             "narrador de vídeo motivacional: ritmo firme, pausas dramáticas nas reticências "
             "e ênfase nas frases curtas.")

MOVS = {  # expressão do zoompan (z = zoom, x/y = posição); on = frame atual, d = total
    "zoom_in":  "z='1+0.12*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
    "zoom_out": "z='1.12-0.12*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
    "pan_left": "z=1.1:x='(iw-iw/zoom)*(1-on/{d})':y='ih/2-(ih/zoom/2)'",
    "pan_right": "z=1.1:x='(iw-iw/zoom)*on/{d}':y='ih/2-(ih/zoom/2)'",
}


def cenas():
    return json.loads((RAIZ / "cenas.json").read_text(encoding="utf-8"))


def regras():
    return "\n\n".join((RAIZ / "prompts" / f).read_text(encoding="utf-8").strip()
                       for f in ("personagem.txt", "cenario.txt", "idioma.txt"))


def prompt_cena(c):
    return (f"{regras()}\n\nFORMATO: composição horizontal 16:9, com o personagem e os textos "
            f"importantes na área central (margens seguras).\n\nCENA: {c['visual']}\n"
            "Mantenha o Rico idêntico à ficha de referência anexada.\n"
            "Não escreva na imagem nomes de pose, expressão, paleta, marca ou destas regras; "
            "só os textos que a CENA pede explicitamente entre aspas.")


def cliente():
    from openai import OpenAI
    return OpenAI()


def imagens(so=None):
    IMG.mkdir(parents=True, exist_ok=True)
    cli = cliente()
    for i, c in enumerate(cenas(), 1):
        dest = IMG / f"{i:02d}.png"
        if dest.exists() or (so and i not in so):
            continue
        print(f"[img] cena {i}")
        with open(RAIZ / "referencia" / "ficha.png", "rb") as ref:
            r = cli.images.edit(model=MODELO_IMG, image=[ref], prompt=prompt_cena(c),
                                size="1536x1024", quality=QUALIDADE_IMG)
        dest.write_bytes(base64.b64decode(r.data[0].b64_json))


def audio():
    AUD.mkdir(parents=True, exist_ok=True)
    cli = cliente()
    for i, c in enumerate(cenas(), 1):
        dest = AUD / f"{i:02d}.mp3"
        if dest.exists():
            continue
        print(f"[tts] cena {i}")
        with cli.audio.speech.with_streaming_response.create(
                model=MODELO_TTS, voice=VOZ, input=c["texto"], instructions=INSTR_TTS) as r:
            r.stream_to_file(dest)


def duracao(arq):
    s = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", str(arq)], text=True)
    return float(s)


def tc(t):
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def blocos(texto, max_pal=7):
    """Quebra o texto em legendas curtas, preferindo cortar em pontuação."""
    frases = re.split(r"(?<=[.!?…:])\s+", texto.strip())
    out = []
    for f in frases:
        pal = f.split()
        n = max(1, -(-len(pal) // max_pal))  # nº de blocos
        tam = -(-len(pal) // n)
        out += [" ".join(pal[k:k + tam]) for k in range(0, len(pal), tam)]
    return out


def montar():
    CLIP.mkdir(parents=True, exist_ok=True)
    srt, t0, lista = [], 0.0, []
    for i, c in enumerate(cenas(), 1):
        img, aud, clip = IMG / f"{i:02d}.png", AUD / f"{i:02d}.mp3", CLIP / f"{i:02d}.mp4"
        dur = duracao(aud) + PAUSA
        frames = int(dur * FPS)
        if not clip.exists():
            print(f"[clip] cena {i} ({dur:.1f}s)")
            zp = MOVS[c.get("mov", "zoom_in")].format(d=frames)
            vf = (f"scale=2880:1920,crop=2880:1620,zoompan={zp}:d={frames}:s={LARG}x{ALT}:fps={FPS},"
                  f"fade=t=in:d=0.2,fade=t=out:st={dur - 0.2:.2f}:d=0.2,format=yuv420p")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-i", str(img),
                            "-i", str(aud), "-vf", vf, "-af", f"apad=pad_dur={PAUSA}",
                            "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", str(clip)], check=True)
        lista.append(f"file '{clip}'")
        # legendas: tempo proporcional ao nº de caracteres de cada bloco
        bl = blocos(c["texto"])
        total = sum(len(b) for b in bl)
        t = t0
        for b in bl:
            d = (dur - PAUSA) * len(b) / total
            srt.append((t, t + d, b))
            t += d
        t0 += dur
    (OUT / "legendas.srt").write_text(
        "\n".join(f"{n}\n{tc(a)} --> {tc(b)}\n{txt}\n" for n, (a, b, txt) in enumerate(srt, 1)),
        encoding="utf-8")
    (OUT / "lista.txt").write_text("\n".join(lista), encoding="utf-8")
    estilo = ("FontName=DejaVu Sans,Bold=1,FontSize=14,PrimaryColour=&H00FFF8F4,"
              "OutlineColour=&H00261408,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=40")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(OUT / "lista.txt"), "-vf",
                    f"subtitles={OUT / 'legendas.srt'}:force_style='{estilo}'",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac",
                    str(OUT / "video_final.mp4")], check=True)
    print("OK ->", OUT / "video_final.mp4", f"({t0 / 60:.1f} min)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("etapa", choices=["imagens", "audio", "montar", "tudo"])
    ap.add_argument("--so", help="números das cenas, ex.: 1,2,3")
    a = ap.parse_args()
    so = {int(x) for x in a.so.split(",")} if a.so else None
    if a.etapa in ("imagens", "tudo"): imagens(so)
    if a.etapa in ("audio", "tudo"): audio()
    if a.etapa in ("montar", "tudo"): montar()
