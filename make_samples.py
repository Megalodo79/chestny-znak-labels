"""Генерирует демонстрационные файлы: шаблон этикетки и PDF с кодами Data Matrix.

Коды выдуманные, но устроены как настоящие коды маркировки: GTIN, серийный номер,
разделитель GS и «криптохвост», который на странице не печатается и существует
только внутри самого Data Matrix. Каждый код нарисован векторными квадратами —
так же, как в файлах, которые выгружает «Честный знак».
"""

from __future__ import annotations

import random
import string
from pathlib import Path

import setuptools  # noqa: F401  (возвращает distutils для pylibdmtx на Python 3.12+)
import pymupdf
from PIL import Image
from pylibdmtx.pylibdmtx import encode



def _font(*candidates: str) -> str:
    """Первый существующий шрифт с кириллицей: Windows, затем Linux."""
    for path in candidates:
        if Path(path).exists():
            return path
    raise SystemExit("Не найден шрифт с кириллицей: укажите путь в make_samples.py")


FONT = _font("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_BOLD = _font("C:/Windows/Fonts/arialbd.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MM = 72 / 25.4

GTIN = "04600000012345"  # выдуманный GTIN для демонстрации
GS = "\x1d"

# Размер модуля в файле кодов, pt. Ядро ищет код по модулям от 2 до 4 pt.
MODULE_PT = 2.5


def make_payload(rng: random.Random) -> tuple[str, str]:
    """Полное содержимое кода и та часть, что печатается под ним."""
    alphabet = string.ascii_letters + string.digits
    serial = "".join(rng.choice(alphabet) for _ in range(13))
    tail = "".join(rng.choice(alphabet) for _ in range(4))
    printed = f"01{GTIN}21{serial}"
    return f"{printed}{GS}93{tail}", printed


def dm_matrix(payload: str) -> list[list[bool]]:
    """Матрица модулей Data Matrix: True — тёмный модуль."""
    encoded = encode(payload.encode("ascii"))
    image = Image.frombytes("RGB", (encoded.width, encoded.height), encoded.pixels)
    # pylibdmtx рисует модуль квадратом 5×5 px с полем в 2 модуля по краям.
    cell, margin = 5, 2
    size = encoded.width // cell - 2 * margin
    return [
        [
            image.getpixel(((margin + col) * cell + 2, (margin + row) * cell + 2))[0] < 128
            for col in range(size)
        ]
        for row in range(size)
    ]


def draw_matrix(page: pymupdf.Page, matrix: list[list[bool]], x: float, y: float) -> None:
    shape = page.new_shape()
    for row, line in enumerate(matrix):
        for col, dark in enumerate(line):
            if dark:
                x0, y0 = x + col * MODULE_PT, y + row * MODULE_PT
                shape.draw_rect(pymupdf.Rect(x0, y0, x0 + MODULE_PT, y0 + MODULE_PT))
                # Каждый модуль — отдельный графический объект, как в выгрузке
                # «Честного знака»: по ним ядро и находит код на странице.
                shape.finish(color=None, fill=(0, 0, 0))
    shape.commit()


def make_codes_pdf(path: Path, count: int, seed: int = 7) -> list[str]:
    """PDF «Честного знака»: одна страница — один код. Возвращает полные payload'ы."""
    rng = random.Random(seed)
    doc = pymupdf.open()
    payloads = []
    for _ in range(count):
        payload, printed = make_payload(rng)
        payloads.append(payload)
        page = doc.new_page(width=58 * MM, height=40 * MM)
        draw_matrix(page, dm_matrix(payload), 8, 10)
        page.insert_textbox(
            pymupdf.Rect(80, 14, page.rect.width - 6, page.rect.height - 6),
            f"{printed[:16]}\n{printed[16:]}",
            fontsize=6.5,
            fontname="arial",
            fontfile=FONT,
        )
    doc.subset_fonts()
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return payloads


def make_template_pdf(path: Path) -> pymupdf.Rect:
    """Этикетка 70×50 мм со свободным местом под код. Возвращает это место."""
    doc = pymupdf.open()
    page = doc.new_page(width=70 * MM, height=50 * MM)
    w, h = page.rect.width, page.rect.height

    page.draw_rect(page.rect, color=None, fill=(0.97, 0.95, 0.90))
    page.draw_rect(pymupdf.Rect(0, 0, w, 30), color=None, fill=(0.16, 0.30, 0.24))
    page.insert_text((10, 20), "ЧАЙ ЧЁРНЫЙ ЛИСТОВОЙ", fontsize=11,
                     fontname="arialbd", fontfile=FONT_BOLD, color=(1, 1, 1))

    lines = [
        "Демо-продукт для примера",
        "Масса нетто: 100 г",
        "Состав: чай чёрный байховый",
        "Хранить в сухом месте",
        "Годен до: 12.2028",
    ]
    for i, line in enumerate(lines):
        page.insert_text((10, 46 + i * 12), line, fontsize=7.5, fontname="arial", fontfile=FONT,
                         color=(0.15, 0.15, 0.15))

    slot = pymupdf.Rect(w - 74, 38, w - 10, 102)
    page.draw_rect(slot, color=(0.55, 0.55, 0.55), width=0.6, dashes="[2 2] 0")
    page.insert_textbox(slot, "\n\nместо\nпод код", fontsize=6.5, fontname="arial",
                        fontfile=FONT, color=(0.55, 0.55, 0.55), align=1)
    doc.subset_fonts()
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return slot


if __name__ == "__main__":
    out = Path(__file__).parent / "samples"
    out.mkdir(exist_ok=True)
    slot = make_template_pdf(out / "label_template.pdf")
    payloads = make_codes_pdf(out / "codes_500.pdf", 500)
    print(f"шаблон: samples/label_template.pdf, место под код {slot}")
    print(f"коды:   samples/codes_500.pdf, {len(payloads)} шт.")
