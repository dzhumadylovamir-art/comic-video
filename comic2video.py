#!/usr/bin/env python3
"""Комикс (папка картинок) -> видео-слайдшоу с японской озвучкой, музыкой и полупрозрачным оверлеем.

Этапы (каждый кэшируется в папке work/, повторный запуск не пересчитывает готовое):
  1. OCR каждой картинки (Apple Vision)        -> work/script.txt  (можно отредактировать вручную!)
  2. Озвучка каждого слайда (VOICEVOX / say)    -> work/audio/*.wav
  3. Кадры 1920x1080 + оверлей                  -> work/frames/*.png
  4. Сборка видео в ffmpeg, музыка по кругу до конца
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageChops, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
MANGA_PY = os.path.expanduser("~/Library/Application Support/ComicVideo/manga-ocr-venv/bin/python")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
W, H = 1920, 1080


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    return float(out.decode().strip())


# ---------------------------------------------------------------- OCR
def order_boxes(boxes, mode):
    """boxes: список dict с x,y,w,h (0..1, y сверху). Порядок чтения реплик."""
    if mode == "ltr":  # обычный текст: строки сверху вниз, внутри строки слева направо
        rows = []
        for b in sorted(boxes, key=lambda b: b["y"] + b["h"] / 2):
            cy = b["y"] + b["h"] / 2
            if rows and abs(cy - rows[-1]["cy"]) < 0.5 * max(b["h"], rows[-1]["h"]):
                rows[-1]["items"].append(b)
            else:
                rows.append({"cy": cy, "h": b["h"], "items": [b]})
        return [b for r in rows for b in sorted(r["items"], key=lambda b: b["x"])]
    else:  # manga: сверху вниз, внутри ряда справа налево
        key = lambda b: (round(b["y"] / 0.12), -(b["x"] + b["w"]))
    return sorted(boxes, key=key)


def clean_line(t):
    t = t.strip()
    # пустые строки и мусор из одних символов-значков пропускаем
    if not re.search(r"[぀-ヿ一-鿿A-Za-z0-9ー]", t):
        return ""
    return t


def ocr_image(path, mode):
    out = subprocess.check_output([os.path.join(HERE, "ocr"), path])
    boxes = order_boxes(json.loads(out), mode)
    text = ""
    for b in boxes:
        line = clean_line(b["text"])
        if not line:
            continue
        if mode != "ltr" and text and not text.endswith(("。", "！", "？", "…", "、", "!", "?")):
            text += "、"
        text += line
    return text


class MangaReader:
    """Держит запущенным manga_worker.py (модель грузится один раз на всю сборку)."""

    def __init__(self):
        print("Загружаю модель manga-ocr...")
        self.p = subprocess.Popen([MANGA_PY, os.path.join(HERE, "manga_worker.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        if "ready" not in json.loads(self.p.stdout.readline() or "{}"):
            raise RuntimeError("manga-ocr не запустился")

    def read(self, path):
        self.p.stdin.write(json.dumps({"image": os.path.abspath(path)}) + "\n")
        res = json.loads(self.p.stdout.readline())
        if "error" in res:
            raise RuntimeError(res["error"])
        parts = []
        for b in res["bubbles"]:
            t = b["text"].replace("．．．", "…").replace("...", "…")
            if not t.endswith(("。", "！", "？", "…", "!", "?", "」", "』", "～", "ー")):
                t += "。"  # пауза между пузырями
            parts.append(t)
        return "".join(parts)

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def write_script(path, items):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Текст озвучки по слайдам. Правьте текст под заголовками [файл].\n"
                "# Пустой текст = слайд без озвучки (покажется --silent-sec секунд).\n\n")
        for name, text in items:
            f.write("[%s]\n%s\n\n" % (name, text))


def read_script(path):
    items, name, buf = [], None, []
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            if name is not None:
                items.append((name, " ".join(buf).strip()))
            name, buf = m.group(1), []
        elif name is not None and not line.startswith("#"):
            buf.append(line.strip())
    if name is not None:
        items.append((name, " ".join(buf).strip()))
    return items


# ---------------------------------------------------------------- TTS
def voicevox_ready(url):
    try:
        urllib.request.urlopen(url + "/version", timeout=2)
        return True
    except Exception:
        return False


def ensure_voicevox(url):
    if voicevox_ready(url):
        return
    print("VOICEVOX не запущен, пробую открыть приложение...")
    if subprocess.call(["open", "-a", "VOICEVOX"], stderr=subprocess.DEVNULL) != 0:
        sys.exit("VOICEVOX не установлен. Скачайте его с https://voicevox.hiroshiba.jp "
                 "или выберите голос macOS в списке голосов.")
    for _ in range(60):
        time.sleep(2)
        if voicevox_ready(url):
            return
    sys.exit("Не удалось подключиться к VOICEVOX (%s). Установите и запустите приложение VOICEVOX "
             "или используйте --tts say." % url)


def tts_voicevox(text, out_wav, url, speaker, speed):
    q = urllib.request.urlopen(urllib.request.Request(
        "%s/audio_query?speaker=%d&text=%s" % (url, speaker, urllib.parse.quote(text)), method="POST")).read()
    query = json.loads(q)
    query["speedScale"] = speed
    req = urllib.request.Request(
        "%s/synthesis?speaker=%d" % (url, speaker), data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with open(out_wav, "wb") as f:
        f.write(urllib.request.urlopen(req, timeout=300).read())


def tts_say(text, out_wav, voice, speed):
    aiff = out_wav + ".aiff"
    run(["say", "-v", voice, "-r", str(int(190 * speed)), "-o", aiff, text])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", aiff, out_wav])
    os.remove(aiff)


# ---------------------------------------------------------------- кадры
def cover(img, w, h, zoom=1.0, ay=0.5):
    s = max(w / img.width, h / img.height) * zoom
    img = img.resize((max(w, round(img.width * s)), max(h, round(img.height * s))), Image.LANCZOS)
    l, t = (img.width - w) // 2, round((img.height - h) * ay)
    return img.crop((l, t, l + w, t + h))


def make_frame(slide_path, overlay, opacity, out_png, blend="multiply", zoom=1.0, ay=0.5, blur=0,
               page_scale=1.0, page_stretch=1.0, page_opacity=1.0):
    src = Image.open(slide_path).convert("RGB")
    if blend == "multiply" and overlay is not None:  # фон цвета бумаги, чтобы фото шло по всему кадру ровно
        bg = Image.new("RGB", (W, H), src.getpixel((2, 2)))
    else:
        bg = cover(src, W, H).filter(ImageFilter.GaussianBlur(40))
        bg = Image.eval(bg, lambda v: int(v * 0.5))
    # page_scale: размер страницы (1 = целиком влезает в кадр, больше — края обрезаются);
    # page_stretch: растяжение по ширине (1 = без искажений)
    s = min(W / src.width, H / src.height) * page_scale
    fg = src.resize((round(src.width * s * page_stretch), round(src.height * s)), Image.LANCZOS)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    if overlay is not None:
        ov = cover(overlay, W, H, zoom, ay)
        if blur:
            ov = ov.filter(ImageFilter.GaussianBlur(blur))
        if blend == "multiply":  # светлые места слайда окрашиваются фото, тёмный текст остаётся тёмным
            mixed = ImageChops.multiply(bg, ov.convert("RGB"))
            bg = Image.blend(bg, mixed, opacity)
        else:
            a = ov.getchannel("A").point(lambda v: int(v * opacity))
            ov.putalpha(a)
            bg = Image.alpha_composite(bg.convert("RGBA"), ov).convert("RGB")
        if page_opacity < 1:  # страница становится прозрачнее, сквозь неё сильнее видно фото
            bg = Image.blend(ov.convert("RGB"), bg, page_opacity)
    bg.save(out_png)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", required=True, help="папка с картинками комикса (порядок — по имени файлов)")
    ap.add_argument("--music", required=False, help="музыкальный файл (mp3/wav/m4a...)")
    ap.add_argument("--overlay", help="картинка-оверлей на весь кадр (PNG с прозрачностью или любая)")
    ap.add_argument("--opacity", type=float, default=0.5, help="сила оверлея 0..1 (по умолчанию 0.5)")
    ap.add_argument("--blend", choices=["multiply", "normal"], default="multiply",
                    help="multiply = фото «вплавлено» в слайд, текст остаётся чётким; normal = обычная прозрачность")
    ap.add_argument("--overlay-zoom", type=float, default=1.0, help="приблизить оверлей (1.3 = на 30%% крупнее)")
    ap.add_argument("--overlay-anchor", type=float, default=0.5, help="какая часть оверлея остаётся в кадре при зуме: 0 = верх, 1 = низ")
    ap.add_argument("--overlay-blur", type=float, default=0, help="размытие оверлея, px")
    ap.add_argument("--page-scale", type=float, default=1.0,
                    help="размер страницы: 1 = целиком в кадре, 1.3 = на 30%% крупнее (края обрежутся)")
    ap.add_argument("--page-opacity", type=float, default=1.0,
                    help="сила страницы 0..1: меньше — страница прозрачнее и сильнее видно фото (нужен --overlay)")
    ap.add_argument("--page-stretch", type=float, default=1.0, help="растянуть страницу по ширине: 1.5 = в 1,5 раза шире")
    ap.add_argument("--out", default="video.mp4")
    ap.add_argument("--work", default=os.path.join(HERE, "work"), help="папка для промежуточных файлов")
    ap.add_argument("--order", choices=["manga", "ltr"], default="manga",
                    help="порядок реплик при OCR: manga = справа налево, ltr = слева направо")
    ap.add_argument("--ocr-engine", choices=["auto", "manga", "vision"], default="auto",
                    help="manga = пузыри + manga-ocr (для комиксов), vision = Apple Vision (для обычного текста), "
                         "auto = manga-ocr, если установлен")
    ap.add_argument("--tts", choices=["voicevox", "say"], default="voicevox")
    ap.add_argument("--speaker", type=int, default=8, help="VOICEVOX speaker id (8 = 春日部つむぎ)")
    ap.add_argument("--voicevox-url", default="http://127.0.0.1:50021")
    ap.add_argument("--say-voice", default="Kyoko")
    ap.add_argument("--speed", type=float, default=1.0, help="скорость речи")
    ap.add_argument("--pad", type=float, default=0.6, help="пауза после озвучки слайда, сек")
    ap.add_argument("--silent-sec", type=float, default=3.0, help="длительность слайда без текста, сек")
    ap.add_argument("--music-volume", type=float, default=0.08)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--ocr-only", action="store_true", help="только распознать текст в work/script.txt и выйти")
    ap.add_argument("--preview", action="store_true", help="только собрать work/preview.png (проверка оверлея)")
    a = ap.parse_args()

    files = sorted([f for f in os.listdir(a.images) if f.lower().endswith(IMG_EXT)], key=natural_key)
    if not files:
        sys.exit("В папке нет картинок")
    overlay = Image.open(a.overlay).convert("RGBA") if a.overlay else None
    for d in ("audio", "frames"):
        os.makedirs(os.path.join(a.work, d), exist_ok=True)

    if a.preview:
        p = os.path.join(a.work, "preview.png")
        make_frame(os.path.join(a.images, files[0]), overlay, a.opacity, p, a.blend, a.overlay_zoom, a.overlay_anchor, a.overlay_blur,
                   a.page_scale, a.page_stretch, a.page_opacity)
        print("Готово:", p)
        return

    # 1. OCR / сценарий
    script_path = os.path.join(a.work, "script.txt")
    texts = dict(read_script(script_path)) if os.path.exists(script_path) else {}
    if texts:
        print("Использую существующий %s (удалите его, чтобы распознать заново)" % script_path)
    todo = [f for f in files if f not in texts]  # распознаём только новые страницы
    use_manga = todo and (a.ocr_engine == "manga" or (a.ocr_engine == "auto" and os.path.exists(MANGA_PY)))
    if a.ocr_engine == "manga" and not os.path.exists(MANGA_PY):
        sys.exit("manga-ocr не установлен. Запустите ./install.sh из папки программы.")
    reader = MangaReader() if use_manga else None
    for i, f in enumerate(todo, 1):
        print("OCR %d/%d %s" % (i, len(todo), f))
        path = os.path.join(a.images, f)
        texts[f] = reader.read(path) if reader else ""
        if not texts[f]:  # пузырей нет (обычная страница с текстом) — Apple Vision
            texts[f] = ocr_image(path, "ltr" if reader else a.order)  # Vision читает только горизонтальный
    if reader:
        reader.close()
    if todo or not os.path.exists(script_path):
        write_script(script_path, [(f, texts.get(f, "")) for f in files])
        print("Текст сохранён в", script_path)
    if a.ocr_only:
        print("Проверьте/поправьте script.txt и запустите снова без --ocr-only.")
        return
    if not a.music:
        print("Внимание: --music не указан, видео будет без музыки.")

    # 2. озвучка + нормализация аудио
    if a.tts == "voicevox" and any(texts.get(f) for f in files):
        ensure_voicevox(a.voicevox_url)
    durs, audio_list = [], []
    for i, f in enumerate(files):
        base = "%04d" % i
        raw = os.path.join(a.work, "audio", base + "_raw.wav")
        seg = os.path.join(a.work, "audio", base + ".wav")
        text = texts.get(f, "")
        stamp = seg + ".txt"  # перегенерируем озвучку, если текст/голос изменились
        sig = json.dumps([text, a.tts, a.speaker, a.say_voice, a.speed, a.pad, a.silent_sec])
        if not (os.path.exists(seg) and os.path.exists(stamp) and open(stamp).read() == sig):
            if text:
                print("TTS %d/%d %s" % (i + 1, len(files), f))
                if a.tts == "voicevox":
                    tts_voicevox(text, raw, a.voicevox_url, a.speaker, a.speed)
                else:
                    tts_say(text, raw, a.say_voice, a.speed)
                run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw,
                     "-af", "aresample=48000,apad=pad_dur=%s" % a.pad, "-ac", "2", "-ar", "48000", seg])
            else:
                run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                     "-t", str(a.silent_sec), seg])
            open(stamp, "w").write(sig)
        durs.append(duration(seg))
        audio_list.append(seg)

    # 3. кадры
    frame_list = []
    ov_sig = "%s|%s|%s|%s|%s|%s|%s|%s|%s" % (a.overlay, a.opacity, a.blend, a.overlay_zoom, a.overlay_anchor,
                                         a.overlay_blur, a.page_scale, a.page_stretch, a.page_opacity)
    ov_stamp = os.path.join(a.work, "frames", "overlay.sig")
    if not (os.path.exists(ov_stamp) and open(ov_stamp).read() == ov_sig):
        for x in os.listdir(os.path.join(a.work, "frames")):
            if x.endswith(".png"):
                os.remove(os.path.join(a.work, "frames", x))
        open(ov_stamp, "w").write(ov_sig)
    for i, f in enumerate(files):
        p = os.path.join(a.work, "frames", "%04d.png" % i)
        if not os.path.exists(p):
            print("Кадр %d/%d" % (i + 1, len(files)))
            make_frame(os.path.join(a.images, f), overlay, a.opacity, p, a.blend, a.overlay_zoom, a.overlay_anchor, a.overlay_blur,
                   a.page_scale, a.page_stretch, a.page_opacity)
        frame_list.append(p)

    # 4. сборка
    img_txt = os.path.join(a.work, "images.txt")
    with open(img_txt, "w") as f:
        for p, d in zip(frame_list, durs):
            f.write("file '%s'\nduration %.4f\n" % (p, d))
        f.write("file '%s'\n" % frame_list[-1])  # требование concat demuxer
    aud_txt = os.path.join(a.work, "audio.txt")
    with open(aud_txt, "w") as f:
        for p in audio_list:
            f.write("file '%s'\n" % p)
    total = sum(durs)
    print("Итоговая длительность: %d:%02d" % (total // 60, total % 60))

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-stats",
           "-f", "concat", "-safe", "0", "-i", img_txt,
           "-f", "concat", "-safe", "0", "-i", aud_txt]
    if a.music:
        cmd += ["-stream_loop", "-1", "-i", a.music]
        fade = max(0.0, total - 3)
        flt = ("[1:a]aresample=48000[v];[2:a]aresample=48000,volume=%s[m];"
               "[v][m]amix=inputs=2:duration=first:dropout_transition=0,volume=2,"
               "afade=t=out:st=%.2f:d=3[a]" % (a.music_volume, fade))
    else:
        flt = "[1:a]aresample=48000[a]"
    cmd += ["-filter_complex", flt, "-map", "0:v", "-map", "[a]",
            "-vf", "fps=%d,format=yuv420p" % a.fps,
            "-c:v", "libx264", "-tune", "stillimage", "-crf", "20", "-preset", "medium",
            "-c:a", "aac", "-b:a", "192k", "-t", "%.3f" % total, "-movflags", "+faststart", a.out]
    run(cmd)
    print("Готово:", os.path.abspath(a.out))


if __name__ == "__main__":
    main()
