"""Ключевые гарантии ядра: код не меняется, не уходит дважды и освобождается при сбое."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chestny_znak as cz  # noqa: E402
import make_samples  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    slot = make_samples.make_template_pdf(tmp_path / "tpl.pdf")
    payloads = make_samples.make_codes_pdf(tmp_path / "codes.pdf", 30, seed=1)
    conn = cz.connect(str(tmp_path / "labels.db"))
    cz.save_template(conn, cz.Template(
        name="demo", pdf_path=str(tmp_path / "tpl.pdf"),
        dm_x=slot.x0 + 2, dm_y=slot.y0 + 2, dm_size=slot.width - 4,
    ))
    return conn, tmp_path, payloads


def test_codes_survive_byte_for_byte(env):
    conn, tmp, payloads = env
    result = cz.build_batch(conn, "demo", str(tmp / "codes.pdf"), 10, out_dir=str(tmp))
    report = cz.verify(result["out_pdf"])
    assert report["unreadable"] == 0
    # Сверяется полное содержимое, включая криптохвост после GS.
    assert report["decoded"] == payloads[:10]


def test_code_is_never_used_twice(env):
    conn, tmp, payloads = env
    first = cz.build_batch(conn, "demo", str(tmp / "codes.pdf"), 10, out_dir=str(tmp))
    second = cz.build_batch(conn, "demo", str(tmp / "codes.pdf"), 10, out_dir=str(tmp))
    a = set(cz.verify(first["out_pdf"])["decoded"])
    b = set(cz.verify(second["out_pdf"])["decoded"])
    assert a.isdisjoint(b)


def test_not_enough_codes_is_refused(env):
    conn, tmp, _ = env
    with pytest.raises(SystemExit, match="Недостаточно кодов"):
        cz.build_batch(conn, "demo", str(tmp / "codes.pdf"), 31, out_dir=str(tmp))


def test_failed_batch_releases_codes(env, monkeypatch):
    conn, tmp, _ = env

    def broken(*args, **kwargs):
        raise RuntimeError("сбой при сборке")

    monkeypatch.setattr(cz, "place_code", broken)
    with pytest.raises(RuntimeError):
        cz.build_batch(conn, "demo", str(tmp / "codes.pdf"), 5, out_dir=str(tmp))

    stats = cz.code_availability(conn, cz.read_codes(str(tmp / "codes.pdf")))
    assert len(stats["available"]) == 30
    status = conn.execute("SELECT status FROM batches").fetchone()[0]
    assert status == "FAILED"


def test_duplicate_pages_in_source_are_counted(env):
    conn, tmp, _ = env
    import pymupdf

    doc = pymupdf.open(str(tmp / "codes.pdf"))
    doc.insert_pdf(pymupdf.open(str(tmp / "codes.pdf")), from_page=0, to_page=2)
    doc.save(str(tmp / "dup.pdf"))
    stats = cz.code_availability(conn, cz.read_codes(str(tmp / "dup.pdf")))
    assert stats["duplicates_in_file"] == 3
    assert len(stats["available"]) == 30
