#!/usr/bin/env python3
"""Локальное окно для comic2video: перетащил картинки -> нажал «Собрать» -> готовое видео.
Сервер слушает только 127.0.0.1, ничего никуда не отправляет."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
JOBS = os.path.expanduser("~/Library/Application Support/ComicVideo/jobs")
OUT_DIR = os.path.expanduser("~/Movies/ComicVideo")
VOICEVOX = "http://127.0.0.1:50021"
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

DEFAULT_SPEAKERS = [(8, "春日部つむぎ"), (2, "四国めたん"), (0, "四国めたん (あまあま)"),
                    (9, "波音リツ"), (14, "冥鳴ひまり"), (20, "もち子さん")]
STATE = {}          # job id -> dict
LOCK = threading.Lock()


def job_dir(job):
    if not re.fullmatch(r"[0-9a-f]{8}", job):
        raise ValueError("bad job")
    return os.path.join(JOBS, job)


LAST = os.path.join(os.path.dirname(JOBS), "last.json")  # последнее собранное видео (для «продолжить»)


def read_last():
    try:
        last = json.load(open(LAST, encoding="utf-8"))
        return last if os.path.isdir(job_dir(last["job"])) else None
    except Exception:
        return None


def unique_out(name):
    """video.mp4 занято -> video2.mp4; video3 занято -> video4 и т.д."""
    m = re.match(r"^(.*?)(\d+)$", name)
    base, n = (m.group(1), int(m.group(2))) if m else (name, 1)
    cand = name
    while os.path.exists(os.path.join(OUT_DIR, cand + ".mp4")):
        n += 1
        cand = "%s%d" % (base, n)
    return os.path.join(OUT_DIR, cand + ".mp4")


def next_idx(d):
    nums = [int(f.split("_", 1)[0]) for f in os.listdir(os.path.join(d, "slides")) if f.split("_", 1)[0].isdigit()]
    return max(nums, default=0) + 1


def merge_into(src, dst):
    """Переносит новые страницы (и фото/музыку, если загружены) из задания src в конец задания dst."""
    s, d = job_dir(src), job_dir(dst)
    for f in sorted(os.listdir(os.path.join(s, "slides"))):
        shutil.move(os.path.join(s, "slides", f), os.path.join(d, "slides", "%04d_%s" % (next_idx(d), f.split("_", 1)[1])))
    for kind in ("overlay", "music"):
        new = [f for f in os.listdir(s) if f.startswith(kind + ".")]
        if new:
            for f in os.listdir(d):
                if f.startswith(kind + "."):
                    os.remove(os.path.join(d, f))
            shutil.move(os.path.join(s, new[0]), os.path.join(d, new[0]))
    shutil.rmtree(s, ignore_errors=True)
    STATE.pop(src, None)


def cleanup_old():
    if not os.path.isdir(JOBS):
        return
    last = read_last()
    for d in os.listdir(JOBS):
        p = os.path.join(JOBS, d)
        if last and d == last["job"]:
            continue
        if time.time() - os.path.getmtime(p) > 7 * 86400:
            shutil.rmtree(p, ignore_errors=True)


def speakers():
    try:
        data = json.load(urllib.request.urlopen(VOICEVOX + "/speakers", timeout=2))
        return [{"id": s["id"], "name": "%s (%s)" % (sp["name"], s["name"])} for sp in data for s in sp["styles"]]
    except Exception:
        return [{"id": i, "name": n} for i, n in DEFAULT_SPEAKERS]


def build_args(job, p, extra):
    d = job_dir(job)
    files = os.listdir(d)
    args = [sys.executable, "-u", os.path.join(HERE, "comic2video.py"),
            "--images", os.path.join(d, "slides"), "--work", os.path.join(d, "work"),
            "--order", p.get("order", "manga"), "--ocr-engine", p.get("ocr_engine", "auto"),
            "--opacity", str(p.get("opacity", 0.5)), "--blend", "multiply",
            "--overlay-zoom", str(p.get("zoom", 1.4)), "--overlay-anchor", "0.15",
            "--overlay-blur", str(p.get("blur", 5)),
            "--page-scale", str(p.get("page_scale", 1.0)), "--page-stretch", str(p.get("page_stretch", 1.0)),
            "--music-volume", str(p.get("music_volume", 0.08)),
            "--speed", str(p.get("speed", 1.0)), "--pad", str(p.get("pad", 0.6))]
    voice = str(p.get("voice", "vv:8"))
    if voice.startswith("say:"):
        args += ["--tts", "say", "--say-voice", voice[4:]]
    else:
        args += ["--tts", "voicevox", "--speaker", voice.split(":")[-1]]
    for kind, flag in (("overlay", "--overlay"), ("music", "--music")):
        f = next((x for x in files if x.startswith(kind + ".")), None)
        if f:
            args += [flag, os.path.join(d, f)]
    return args + extra


def run_job(job, p, phase):
    st = STATE[job]
    d = job_dir(job)
    os.makedirs(OUT_DIR, exist_ok=True)
    name = re.sub(r"[^\w\-. ]", "_", p.get("name") or "video").strip() or "video"
    # при продолжении перезаписываем предыдущее видео, иначе берём свободное имя
    out = st.get("append_out") or unique_out(name)
    extra = ["--ocr-only"] if phase == "ocr" else ["--out", out]
    with LOCK:
        st.update(state="running", stage=phase, log=[], progress=0.0, output=None, params=p)
    try:
        proc = subprocess.Popen(build_args(job, p, extra), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in proc.stdout:
            line = line.strip()
            if not line or line.startswith("frame="):
                continue
            m = re.match(r"(OCR|TTS|Кадр) (\d+)/(\d+)", line)
            with LOCK:
                st["log"].append(line)
                if m:
                    base = {"OCR": 0.0, "TTS": 0.0 if phase == "ocr" else 0.05, "Кадр": 0.7}[m.group(1)]
                    span = 1.0 if phase == "ocr" else (0.65 if m.group(1) == "TTS" else 0.15)
                    st["progress"] = base + span * int(m.group(2)) / int(m.group(3))
                elif line.startswith("Итоговая"):
                    st["progress"] = 0.88
        code = proc.wait()
    except Exception as e:  # noqa: BLE001
        with LOCK:
            st.update(state="error", log=st["log"] + [str(e)])
        return
    with LOCK:
        if code != 0:
            st["state"] = "error"
        elif phase == "ocr":
            st["script"] = open(os.path.join(d, "work", "script.txt"), encoding="utf-8").read()
            st["state"] = "review"
        else:
            st.update(state="done", output=out, progress=1.0)
            slides = len(os.listdir(os.path.join(d, "slides")))
            with open(LAST, "w", encoding="utf-8") as f:
                json.dump({"job": job, "output": out, "slides": slides}, f, ensure_ascii=False)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                b = open(os.path.join(HERE, "index.html"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif u.path == "/api/last":
                last = read_last()
                if last:
                    last["name"] = os.path.basename(last["output"])
                    last["exists"] = os.path.exists(last["output"])
                self.send_json(last or {})
            elif u.path == "/api/speakers":
                self.send_json(speakers())
            elif u.path == "/api/status":
                with LOCK:
                    self.send_json(STATE.get(q["job"][0], {"state": "idle"}))
            elif u.path == "/api/preview.png":
                b = open(os.path.join(job_dir(q["job"][0]), "work", "preview.png"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            else:
                self.send_error(404)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/new":
                job = uuid.uuid4().hex[:8]
                os.makedirs(os.path.join(job_dir(job), "slides"))
                STATE[job] = {"state": "idle"}
                self.send_json({"job": job})
            elif u.path == "/api/upload":
                job, kind = q["job"][0], q["kind"][0]
                name = os.path.basename(q["name"][0])
                ext = os.path.splitext(name)[1].lower()
                data, d = self.body(), job_dir(job)
                if kind == "slide":
                    idx = next_idx(d)  # страницы идут в порядке загрузки
                    if ext == ".pdf":
                        tmp = os.path.join(d, "pdf_%d" % idx)
                        os.makedirs(tmp, exist_ok=True)
                        pdf = os.path.join(tmp, "in.pdf")
                        open(pdf, "wb").write(data)
                        subprocess.run([os.path.join(HERE, "pdf2png"), pdf, os.path.join(tmp, "p"), "1800"], check=True)
                        for f in sorted(os.listdir(os.path.join(tmp, "p"))):
                            shutil.move(os.path.join(tmp, "p", f), os.path.join(d, "slides", "%04d_%s" % (idx, f)))
                        shutil.rmtree(tmp)
                    else:
                        open(os.path.join(d, "slides", "%04d_%s" % (idx, name)), "wb").write(data)
                else:  # overlay / music: один файл, заменяем предыдущий
                    for f in os.listdir(d):
                        if f.startswith(kind + "."):
                            os.remove(os.path.join(d, f))
                    open(os.path.join(d, kind + ext), "wb").write(data)
                self.send_json({"ok": True})
            elif u.path == "/api/preview":
                req = json.loads(self.body())
                job, d = req["job"], job_dir(req["job"])
                os.makedirs(os.path.join(d, "work"), exist_ok=True)
                args = build_args(job, req["params"], ["--preview"])
                r = subprocess.run(args, capture_output=True, text=True)
                self.send_json({"ok": r.returncode == 0, "log": r.stdout + r.stderr})
            elif u.path == "/api/start":
                req = json.loads(self.body())
                job = req["job"]
                if STATE.get(job, {}).get("state") == "running":
                    return self.send_json({"ok": False})
                append_out = None
                if req.get("append"):
                    last = read_last()
                    if not last:
                        return self.send_json({"ok": False, "error": "Предыдущее видео не найдено"})
                    if last["job"] != job:
                        merge_into(job, last["job"])
                        job = last["job"]
                    append_out = last["output"]
                else:
                    shutil.rmtree(os.path.join(job_dir(job), "work"), ignore_errors=True)
                STATE[job] = {"state": "running", "log": [], "progress": 0, "append_out": append_out}
                phase = "ocr" if req.get("review") else "all"
                threading.Thread(target=run_job, args=(job, req["params"], phase), daemon=True).start()
                self.send_json({"ok": True, "job": job})
            elif u.path == "/api/continue":
                req = json.loads(self.body())
                job = req["job"]
                with open(os.path.join(job_dir(job), "work", "script.txt"), "w", encoding="utf-8") as f:
                    f.write(req["script"])
                threading.Thread(target=run_job, args=(job, req["params"], "all"), daemon=True).start()
                self.send_json({"ok": True})
            elif u.path == "/api/reveal":
                subprocess.call(["open", "-R", json.loads(self.body())["path"]])
                self.send_json({"ok": True})
            elif u.path == "/api/quit":
                self.send_json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0))).start()
            else:
                self.send_error(404)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)


def main():
    cleanup_old()
    os.makedirs(JOBS, exist_ok=True)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:  # уже запущено — просто открываем окно
        webbrowser.open("http://127.0.0.1:%d" % PORT)
        return
    if not os.environ.get("CV_NO_BROWSER"):
        webbrowser.open("http://127.0.0.1:%d" % PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
