#!/usr/bin/env python3
"""Распознавание манги: находит пузыри с текстом, определяет порядок чтения (панели сверху вниз,
справа налево) и читает каждый пузырь моделью manga-ocr.

Запускается python'ом из окружения manga-ocr. Протокол: по строке JSON на stdin
{"image": путь} -> по строке JSON на stdout {"bubbles": [{"box": [x0, y0, x1, y1], "text": "..."}]}.
Пустой список = пузырей не найдено (например, обычная страница с текстом)."""
import functools
import json
import os
import sys

import cv2
import numpy as np
from PIL import Image

# библиотеки любят печатать в stdout — уводим их в stderr, ответы пишем в настоящий stdout
OUT = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def read_gray(path):
    data = np.fromfile(path, dtype=np.uint8)  # imread не любит не-ASCII пути
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:  # например, webp без поддержки — через PIL
        img = np.array(Image.open(path).convert("L"))
    return img


def fill_holes(mask):
    h, w = mask.shape
    pad = np.zeros((h + 2, w + 2), np.uint8)
    pad[1:-1, 1:-1] = mask
    flood = pad.copy()
    cv2.floodFill(flood, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 1)
    return (pad | (1 - flood))[1:-1, 1:-1]


def find_bubbles(g):
    """Белые замкнутые области, внутри которых много мелких тёмных пятен (иероглифов)."""
    H, W = g.shape
    white = (g > 200).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=4)
    out = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 0.0015 * H * W or bw < 0.03 * W or bh < 0.03 * H:
            continue
        if (bw > 0.6 * W and bh > 0.6 * H) or x == 0 or y == 0 or x + bw >= W or y + bh >= H:
            continue  # поля страницы и всё, что касается края
        comp = (labels[y:y + bh, x:x + bw] == i).astype(np.uint8)
        if comp.sum() / float(bw * bh) < 0.35:
            continue  # тонкие полоски штриховки
        filled = fill_holes(comp)
        holes = filled & (1 - comp)
        hole_area = int(holes.sum())
        if not (0.01 < hole_area / float(filled.sum()) < 0.5):
            continue
        k, _, hs, _ = cv2.connectedComponentsWithStats(holes, connectivity=8)
        small = [s for s in hs[1:] if s[4] >= 3 and s[4] < 0.02 * filled.sum()]
        if len(small) < 4 or sum(s[4] for s in small) < 0.6 * hole_area:
            continue  # внутри не текст, а картинка (лицо, другие пузыри)
        # рамка по самим знакам, а не по всему пузырю
        tx0 = min(s[0] for s in small); ty0 = min(s[1] for s in small)
        tx1 = max(s[0] + s[2] for s in small); ty1 = max(s[1] + s[3] for s in small)
        out.append({"box": [int(x), int(y), int(x + bw), int(y + bh)],
                    "text_box": [int(x + tx0), int(y + ty0), int(x + tx1), int(y + ty1)],
                    "mask": filled})
    return out


def page_background(g):
    """Белый фон, связанный с краями страницы: поля и промежутки между панелями."""
    white = (g > 200).astype(np.uint8)
    n, labels = cv2.connectedComponents(white, connectivity=4)
    edge = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    return np.isin(labels, edge[edge > 0])


def xy_cut(bg, x0, y0, x1, y1, horizontal=True, depth=0):
    """Делит страницу на панели по промежуткам (строка/столбец, где больше половины — фон).
    Пузырь, заходящий на промежуток, его не ломает. Порядок: сверху вниз, справа налево."""
    region = bg[y0:y1, x0:x1]
    if region.size == 0 or region.all():
        return []
    for direction in ((horizontal, not horizontal) if depth < 8 else ()):
        gap = region.mean(axis=1 if direction else 0) >= 0.5
        segs, start = [], None
        for i, gp in enumerate(list(gap) + [True]):
            if not gp and start is None:
                start = i
            elif gp and start is not None:
                segs.append((start, i))
                start = None
        segs = [s for s in segs if s[1] - s[0] > 0.03 * len(gap)]
        if len(segs) > 1:
            res = []
            if not direction:
                segs = segs[::-1]  # колонки панелей — справа налево
            for a, b in segs:
                if direction:
                    res += xy_cut(bg, x0, y0 + a, x1, y0 + b, not direction, depth + 1)
                else:
                    res += xy_cut(bg, x0 + a, y0, x0 + b, y1, not direction, depth + 1)
            return res
    return [(x0, y0, x1, y1)]


def reading_order(bubbles, g):
    H, W = g.shape
    panels = xy_cut(page_background(g), 0, 0, W, H) or [(0, 0, W, H)]

    def panel_of(b):
        # панель, где начинается текст: верх самой правой колонки
        tx0, ty0, tx1, ty1 = b["text_box"]
        px, py = tx1 - 0.1 * (tx1 - tx0), ty0 + 0.1 * (ty1 - ty0)

        def dist(r):
            return max(r[0] - px, 0, px - r[2]) + max(r[1] - py, 0, py - r[3])
        return min(range(len(panels)), key=lambda i: dist(panels[i]))

    def cmp(a, b):
        pa, pb = panel_of(a), panel_of(b)
        if pa != pb:
            return pa - pb
        (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = a["text_box"], b["text_box"]
        if min(ay1, by1) - max(ay0, by0) > 0:  # на одной высоте — правый раньше
            return -1 if ax1 > bx1 else 1
        return -1 if ay0 < by0 else 1

    return sorted(bubbles, key=functools.cmp_to_key(cmp))


def crop(gray_full, scale, b):
    x0, y0, x1, y1 = b["box"]
    m = cv2.resize(b["mask"], None, fx=1 / scale, fy=1 / scale, interpolation=cv2.INTER_NEAREST) if scale != 1 else b["mask"]
    X0, Y0 = int(x0 / scale), int(y0 / scale)
    c = gray_full[Y0:Y0 + m.shape[0], X0:X0 + m.shape[1]].copy()
    m = m[:c.shape[0], :c.shape[1]]
    c[m == 0] = 255  # всё вне пузыря — белым, чтобы не мешал рисунок
    tx0, ty0, tx1, ty1 = [int(v / scale) for v in b["text_box"]]
    pad = max(8, (ty1 - ty0) // 20)
    c = c[max(0, ty0 - Y0 - pad):ty1 - Y0 + pad, max(0, tx0 - X0 - pad):tx1 - X0 + pad]
    return Image.fromarray(c).convert("RGB")


def main():
    from manga_ocr import MangaOcr
    mocr = MangaOcr()
    OUT.write(json.dumps({"ready": True}) + "\n")
    for line in sys.stdin:
        try:
            req = json.loads(line)
            full = read_gray(req["image"])
            scale = min(1.0, 1600.0 / max(full.shape))
            g = cv2.resize(full, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else full
            res = []
            for b in reading_order(find_bubbles(g), g):
                text = mocr(crop(full, scale, b)).strip()
                if text:
                    res.append({"box": [int(v / scale) for v in b["box"]], "text": text})
            OUT.write(json.dumps({"bubbles": res}, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            OUT.write(json.dumps({"error": str(e)}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
