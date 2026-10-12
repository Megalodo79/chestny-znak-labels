"""
Формирование этикеток с уникальными кодами «Честный знак».

Прототип ядра: шаблон этикетки (PDF) + файл кодов Data Matrix (PDF) -> тираж этикеток.

Принцип: оригинальный Data Matrix переносится из исходного PDF как векторный объект
(XObject). Код не распознаётся и не генерируется заново, поэтому его содержимое,
включая криптохвост, физически не может измениться.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import sqlite3
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import pymupdf
from PIL import Image

# Размер модуля Data Matrix на исходной странице, pt. Модули ищутся по габаритам,
# чтобы отделить сам код от текста и рамок на странице.
MODULE_MIN_PT = 2.0
MODULE_MAX_PT = 4.0

# Свободное поле вокруг кода. Без него сканер код не читает — проверено на образцах.
QUIET_ZONE_MODULES = 2

DB_PATH = "labels.db"


# --------------------------------------------------------------------------- модели


@dataclass
class Template:
    """Настройка шаблона этикетки: где на ней стоит Data Matrix."""

    name: str
    pdf_path: str
    dm_x: float  # верхний левый угол области кода, pt (отсчёт сверху вниз, как в PDF)
    dm_y: float
    dm_size: float  # сторона квадрата под код, pt
    rotate: int = 0
    backdrop: bool = True  # белая подложка под кодом
    backdrop_margin: float = 4.0  # поля подложки вокруг кода, pt
    # Явные габариты подложки (x0, y0, x1, y1). Задаются, когда код должен закрыть
    # существующий элемент этикетки целиком — логотип, картинку, старый штрихкод.
    backdrop_rect: tuple[float, float, float, float] | None = None


@dataclass
class SourceCode:
    """Один код из файла «Честного знака»."""

    page: int
    text: str  # человекочитаемая часть, напечатанная на странице
    bbox: tuple[float, float, float, float]  # габариты самого Data Matrix
    module: float  # размер одного модуля кода, pt — от него считается quiet zone
    digest: str  # идентификатор кода для базы использованных


# --------------------------------------------------------------------------- база


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS templates (
            name TEXT PRIMARY KEY,
            config TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS codes (
            digest TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            page INTEGER NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL,          -- RESERVED | USED
            batch_id TEXT,
            used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY,
            template TEXT NOT NULL,
            source TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            out_pdf TEXT,
            operator TEXT,
            status TEXT NOT NULL           -- RESERVED | SUCCESS | FAILED
        );
        """
    )
    return conn


def save_template(conn: sqlite3.Connection, tpl: Template) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO templates (name, config) VALUES (?, ?)",
        (tpl.name, json.dumps(asdict(tpl), ensure_ascii=False)),
    )
    conn.commit()


def load_template(conn: sqlite3.Connection, name: str) -> Template:
    row = conn.execute("SELECT config FROM templates WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise SystemExit(f"Шаблон «{name}» не найден. Сначала выполните add-template.")
    return Template(**json.loads(row[0]))


# --------------------------------------------------------------------- чтение кодов


def find_dm(page: pymupdf.Page) -> tuple[pymupdf.Rect, float] | None:
    """Габариты Data Matrix на странице и размер его модуля.

    Код ищется как объединение квадратных модулей. Размер модуля берётся минимальный
    из найденных: соседние модули иногда склеены в один прямоугольник, но меньше
    одного модуля элемент кода быть не может.
    """
    box, module = None, None
    for drawing in page.get_drawings():
        rect = pymupdf.Rect(drawing["rect"])
        if MODULE_MIN_PT <= rect.width <= MODULE_MAX_PT and MODULE_MIN_PT <= rect.height <= MODULE_MAX_PT:
            box = rect if box is None else (box | rect)
            side = min(rect.width, rect.height)
            module = side if module is None else min(module, side)
    if box is None:
        return None
    return box, module


def quiet_clip(bbox: pymupdf.Rect, module: float) -> pymupdf.Rect:
    """Область кода вместе со свободным полем вокруг него."""
    pad = module * QUIET_ZONE_MODULES
    return pymupdf.Rect(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad)


def read_codes(codes_pdf: str) -> list[SourceCode]:
    """Читает все коды из PDF «Честного знака». Одна страница — один код."""
    doc = pymupdf.open(codes_pdf)
    codes: list[SourceCode] = []

    for index, page in enumerate(doc):
        found = find_dm(page)
        if found is None:
            print(f"  ! страница {index + 1}: Data Matrix не найден, пропущена", file=sys.stderr)
            continue
        bbox, module = found
        text = " ".join(page.get_text().split())
        # Идентификатор кода привязан к его содержимому, а не к имени файла:
        # повторная загрузка того же PDF даст те же digest'ы.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        codes.append(SourceCode(index, text, tuple(bbox), module, digest))

    doc.close()
    return codes


def code_availability(conn: sqlite3.Connection, codes: list[SourceCode]) -> dict:
    """Сводка: всего / уникальных / использовано ранее / доступно."""
    digests = [c.digest for c in codes]
    unique = set(digests)

    used = set()
    if unique:
        placeholders = ",".join("?" * len(unique))
        rows = conn.execute(
            f"SELECT digest FROM codes WHERE digest IN ({placeholders}) AND status IN ('USED','RESERVED')",
            tuple(unique),
        ).fetchall()
        used = {r[0] for r in rows}

    available = [c for c in codes if c.digest not in used]
    # Дубли внутри самого файла тоже отсеиваем, оставляя первое вхождение.
    seen, deduped = set(), []
    for code in available:
        if code.digest not in seen:
            seen.add(code.digest)
            deduped.append(code)

    return {
        "total": len(codes),
        "unique": len(unique),
        "used_before": len(used),
        "available": deduped,
        "duplicates_in_file": len(digests) - len(unique),
    }


# ------------------------------------------------------------------- сборка тиража


def place_code(
    out_page: pymupdf.Page,
    codes_doc: pymupdf.Document,
    code: SourceCode,
    tpl: Template,
) -> None:
    """Переносит Data Matrix на этикетку без растеризации и перегенерации."""
    target = pymupdf.Rect(tpl.dm_x, tpl.dm_y, tpl.dm_x + tpl.dm_size, tpl.dm_y + tpl.dm_size)

    if tpl.backdrop:
        if tpl.backdrop_rect is not None:
            backdrop = pymupdf.Rect(*tpl.backdrop_rect)
        else:
            margin = tpl.backdrop_margin
            backdrop = pymupdf.Rect(
                target.x0 - margin, target.y0 - margin, target.x1 + margin, target.y1 + margin
            )
        # Подложка не должна вылезать за обрез этикетки.
        backdrop &= out_page.rect
        out_page.draw_rect(backdrop, color=None, fill=(1, 1, 1), overlay=True)

    clip = quiet_clip(pymupdf.Rect(*code.bbox), code.module)

    out_page.show_pdf_page(target, codes_doc, code.page, clip=clip, rotate=tpl.rotate, overlay=True)


def build_batch(
    conn: sqlite3.Connection,
    template_name: str,
    codes_pdf: str,
    quantity: int,
    out_dir: str = ".",
    operator: str = "operator",
) -> dict:
    """Формирует тираж: резервирует коды, собирает PDF, фиксирует партию."""
    tpl = load_template(conn, template_name)
    codes = read_codes(codes_pdf)
    stats = code_availability(conn, codes)
    available = stats["available"]

    if quantity > len(available):
        raise SystemExit(
            f"Недостаточно кодов. Доступно: {len(available)}, требуется: {quantity}."
        )

    selected = available[:quantity]
    batch_id = f"B-{dt.datetime.now():%Y%m%d-%H%M%S-%f}"
    now = dt.datetime.now().isoformat(timespec="seconds")

    # Шаг 1: RESERVED. Коды заняты, но партия ещё не подтверждена.
    conn.execute(
        "INSERT INTO batches (batch_id, template, source, quantity, created_at, operator, status)"
        " VALUES (?,?,?,?,?,?,'RESERVED')",
        (batch_id, tpl.name, Path(codes_pdf).name, quantity, now, operator),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO codes (digest, source, page, text, status, batch_id, used_at)"
        " VALUES (?,?,?,?,'RESERVED',?,NULL)",
        [(c.digest, Path(codes_pdf).name, c.page, c.text, batch_id) for c in selected],
    )
    conn.commit()

    try:
        template_doc = pymupdf.open(tpl.pdf_path)
        codes_doc = pymupdf.open(codes_pdf)
        out_doc = pymupdf.open()
        page_rect = template_doc[0].rect

        for code in selected:
            # Шаблон вставляется ссылкой на один общий XObject, а не копируется на каждую
            # страницу: иначе тираж в 1000 шт. весил бы больше гигабайта.
            page = out_doc.new_page(width=page_rect.width, height=page_rect.height)
            page.show_pdf_page(page.rect, template_doc, 0)
            place_code(page, codes_doc, code, tpl)

        safe_name = tpl.name.replace(" ", "_")
        out_path = str(Path(out_dir) / f"{safe_name}_{quantity}шт_{batch_id}.pdf")
        out_doc.save(out_path, garbage=4, deflate=True)
        out_doc.close()
        template_doc.close()
        codes_doc.close()
    except Exception:
        # Шаг 2-: сборка не удалась — коды освобождаются, партия помечается FAILED.
        conn.execute("DELETE FROM codes WHERE batch_id = ?", (batch_id,))
        conn.execute("UPDATE batches SET status='FAILED' WHERE batch_id = ?", (batch_id,))
        conn.commit()
        raise

    # Шаг 2+: PDF собран — фиксируем партию и переводим коды в USED.
    conn.execute(
        "UPDATE batches SET status='SUCCESS', out_pdf=? WHERE batch_id=?", (out_path, batch_id)
    )
    conn.execute(
        "UPDATE codes SET status='USED', used_at=? WHERE batch_id=?", (now, batch_id)
    )
    conn.commit()

    return {"batch_id": batch_id, "out_pdf": out_path, "quantity": quantity, "stats": stats}


# ---------------------------------------------------------------------- проверка


def verify(out_pdf: str, decode_codes: bool = True) -> dict:
    """Проверяет готовый тираж: количество, уникальность и читаемость кодов."""
    doc = pymupdf.open(out_pdf)
    report = {
        "pages": doc.page_count,
        "codes_found": 0,
        "unique": 0,
        "duplicates": 0,
        "unreadable": 0,
        "decoded": [],
    }

    decoder = None
    if decode_codes:
        try:
            import setuptools  # noqa: F401  (возвращает distutils для pylibdmtx на Python 3.12+)
            from pylibdmtx.pylibdmtx import decode as decoder
        except Exception:
            decoder = None
            report["decode_skipped"] = "pylibdmtx недоступен"

    seen = set()
    for page in doc:
        found = find_dm(page)
        if found is None:
            report["unreadable"] += 1
            continue
        report["codes_found"] += 1
        bbox, module = found

        if decoder is None:
            continue

        pixmap = page.get_pixmap(dpi=300, clip=quiet_clip(bbox, module))
        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
        result = decoder(image, timeout=10000, max_count=1)

        if not result:
            report["unreadable"] += 1
            continue
        payload = result[0].data.decode("utf-8", "replace")
        report["decoded"].append(payload)
        if payload in seen:
            report["duplicates"] += 1
        seen.add(payload)

    report["unique"] = len(seen)
    doc.close()
    return report


def preview(template_name: str, codes_pdf: str, out_png: str = "preview.png", db: str = DB_PATH) -> str:
    """Рендерит первую этикетку тиража, не расходуя коды."""
    conn = connect(db)
    tpl = load_template(conn, template_name)
    codes = read_codes(codes_pdf)
    available = code_availability(conn, codes)["available"]
    if not available:
        raise SystemExit("Нет доступных кодов для предпросмотра.")

    template_doc = pymupdf.open(tpl.pdf_path)
    codes_doc = pymupdf.open(codes_pdf)
    out_doc = pymupdf.open()
    page = out_doc.new_page(width=template_doc[0].rect.width, height=template_doc[0].rect.height)
    page.show_pdf_page(page.rect, template_doc, 0)
    place_code(page, codes_doc, available[0], tpl)
    page.get_pixmap(dpi=200).save(out_png)
    out_doc.close()
    return out_png


# --------------------------------------------------------------------------- CLI


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-template", help="сохранить шаблон и область под Data Matrix")
    p.add_argument("name")
    p.add_argument("pdf")
    p.add_argument("--x", type=float, required=True)
    p.add_argument("--y", type=float, required=True)
    p.add_argument("--size", type=float, required=True)
    p.add_argument("--rotate", type=int, default=0)
    p.add_argument("--no-backdrop", action="store_true")

    p = sub.add_parser("inspect", help="показать сводку по файлу кодов")
    p.add_argument("codes_pdf")

    p = sub.add_parser("preview", help="предпросмотр первой этикетки")
    p.add_argument("template")
    p.add_argument("codes_pdf")
    p.add_argument("--out", default="preview.png")

    p = sub.add_parser("build", help="сформировать тираж")
    p.add_argument("template")
    p.add_argument("codes_pdf")
    p.add_argument("quantity", type=int)
    p.add_argument("--out-dir", default=".")

    p = sub.add_parser("verify", help="проверить готовый тираж")
    p.add_argument("out_pdf")
    p.add_argument("--no-decode", action="store_true")

    args = parser.parse_args()
    conn = connect(args.db)

    if args.command == "add-template":
        tpl = Template(
            name=args.name,
            pdf_path=args.pdf,
            dm_x=args.x,
            dm_y=args.y,
            dm_size=args.size,
            rotate=args.rotate,
            backdrop=not args.no_backdrop,
        )
        save_template(conn, tpl)
        print(f"Шаблон «{tpl.name}» сохранён: код {tpl.dm_size:.0f}×{tpl.dm_size:.0f} pt "
              f"в точке ({tpl.dm_x:.0f}, {tpl.dm_y:.0f}).")

    elif args.command == "inspect":
        codes = read_codes(args.codes_pdf)
        stats = code_availability(conn, codes)
        print(f"Файл: {Path(args.codes_pdf).name}")
        print(f"  всего кодов:        {stats['total']}")
        print(f"  уникальных:         {stats['unique']}")
        print(f"  дублей в файле:     {stats['duplicates_in_file']}")
        print(f"  использовано ранее: {stats['used_before']}")
        print(f"  доступно:           {len(stats['available'])}")

    elif args.command == "preview":
        path = preview(args.template, args.codes_pdf, args.out, args.db)
        print(f"Предпросмотр сохранён: {path}")

    elif args.command == "build":
        result = build_batch(conn, args.template, args.codes_pdf, args.quantity, args.out_dir)
        stats = result["stats"]
        print(f"Партия {result['batch_id']}: {result['quantity']} этикеток")
        print(f"  доступно было: {len(stats['available'])} из {stats['total']}")
        print(f"  файл: {result['out_pdf']}")

    elif args.command == "verify":
        report = verify(args.out_pdf, decode_codes=not args.no_decode)
        print(f"Проверка {Path(args.out_pdf).name}:")
        print(f"  страниц:    {report['pages']}")
        print(f"  кодов:      {report['codes_found']}")
        print(f"  уникальных: {report['unique']}")
        print(f"  дублей:     {report['duplicates']}")
        print(f"  нечитаемых: {report['unreadable']}")
        if report.get("decode_skipped"):
            print(f"  (декодирование пропущено: {report['decode_skipped']})")


if __name__ == "__main__":
    main()
