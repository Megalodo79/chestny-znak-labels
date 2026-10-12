"""Полный прогон на демонстрационных файлах — от шаблона до проверки тиража."""

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

import pymupdf  # noqa: E402

import chestny_znak as cz  # noqa: E402
import make_samples  # noqa: E402

TEMPLATE_PDF = "samples/label_template.pdf"
CODES_PDF = "samples/codes_500.pdf"
TEMPLATE_NAME = "Чай чёрный 100 г"
OUT_DIR = Path("out")


def step(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# Чистый прогон: демо-файлы и база создаются заново, чтобы результат был воспроизводимым.
Path("samples").mkdir(exist_ok=True)
slot = make_samples.make_template_pdf(Path(TEMPLATE_PDF))
payloads = make_samples.make_codes_pdf(Path(CODES_PDF), 500)
OUT_DIR.mkdir(exist_ok=True)
for old in OUT_DIR.glob("*.pdf"):
    old.unlink()
Path(cz.DB_PATH).unlink(missing_ok=True)
conn = cz.connect()

step("1. Регистрация шаблона")
tpl = cz.Template(
    name=TEMPLATE_NAME,
    pdf_path=TEMPLATE_PDF,
    dm_x=slot.x0 + 2,
    dm_y=slot.y0 + 2,
    dm_size=slot.width - 4,
    # Подложка закрывает пунктирную рамку «место под код» целиком.
    backdrop_rect=(slot.x0 - 1, slot.y0 - 1, slot.x1 + 1, slot.y1 + 1),
)
cz.save_template(conn, tpl)
print(f"Шаблон «{tpl.name}»: код {tpl.dm_size / 72 * 25.4:.1f} мм "
      f"в точке ({tpl.dm_x:.0f}, {tpl.dm_y:.0f}) pt")

step("2. Разбор файла кодов")
codes = cz.read_codes(CODES_PDF)
stats = cz.code_availability(conn, codes)
print(f"всего кодов:        {stats['total']}")
print(f"уникальных:         {stats['unique']}")
print(f"дублей в файле:     {stats['duplicates_in_file']}")
print(f"использовано ранее: {stats['used_before']}")
print(f"доступно:           {len(stats['available'])}")
print(f"\nнапечатано под первым кодом: {codes[0].text}")
print(f"полное содержимое кода:      {payloads[0]!r}")
print("  (часть после \\x1d93 — «криптохвост», на странице его нет)")

step("3. Предпросмотр первой этикетки")
print("сохранён:", cz.preview(TEMPLATE_NAME, CODES_PDF, "preview.png"))

step("4. Сборка тиража — 20 шт.")
result = cz.build_batch(conn, TEMPLATE_NAME, CODES_PDF, 20, out_dir=str(OUT_DIR))
print(f"партия: {result['batch_id']}")
print(f"файл:   {result['out_pdf']}")
print(f"размер: {Path(result['out_pdf']).stat().st_size / 1024:.0f} КБ")

step("5. Проверка тиража сканером: криптохвост сохранился")
report = cz.verify(result["out_pdf"])
print(f"страниц {report['pages']}, кодов {report['codes_found']}, уникальных {report['unique']}, "
      f"дублей {report['duplicates']}, нечитаемых {report['unreadable']}")
match = report["decoded"] == payloads[:20]
print(f"содержимое совпало с исходным байт в байт: {'ДА' if match else 'НЕТ'}")

step("6. Защита от повторного использования")
after = cz.code_availability(conn, cz.read_codes(CODES_PDF))
print(f"использовано {after['used_before']}, доступно {len(after['available'])}")
try:
    cz.build_batch(conn, TEMPLATE_NAME, CODES_PDF, 500, out_dir=str(OUT_DIR))
except SystemExit as exc:
    print(f"попытка взять 500 шт. → {exc}")

step("7. Масштаб: партия 400 шт.")
started = time.perf_counter()
big = cz.build_batch(conn, TEMPLATE_NAME, CODES_PDF, 400, out_dir=str(OUT_DIR))
elapsed = time.perf_counter() - started
big_report = cz.verify(big["out_pdf"])
print(f"400 этикеток за {elapsed:.1f} с, файл {Path(big['out_pdf']).stat().st_size / 1024 / 1024:.1f} МБ")
print(f"проверка: уникальных {big_report['unique']}, дублей {big_report['duplicates']}, "
      f"нечитаемых {big_report['unreadable']}")
print(f"совпадение с исходными кодами: "
      f"{'ДА' if big_report['decoded'] == payloads[20:420] else 'НЕТ'}")

# Картинка для README: первая страница тиража крупно.
pymupdf.open(result["out_pdf"])[0].get_pixmap(dpi=220).save("preview.png")
print("\nГотово.")
