import zipfile

from openpyxl import Workbook

from kais_scraper.extract import run_extract
from kais_scraper.ownership import run_ownership
from kais_scraper.paths import Layout
from kais_scraper.timewindow import in_window, parse_window


def test_extract_and_ownership(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()

    wb = Workbook()
    ws = wb.active
    ws.append(["Идентификатор", "Собственик\nи дял", None])
    ws.append(["00151.1.1", "Иван   Петров\n1/2", None])
    ws.append([None, None, None])
    xlsx = tmp_path / "src.xlsx"
    wb.save(xlsx)

    with zipfile.ZipFile(layout.doc / "Бургас-Айтос-Айтос-00151-00151.zip", "w") as zf:
        zf.write(xlsx, "00151 ПИ.xlsx")
        zf.writestr("../evil.txt", "nope")

    assert run_extract(layout, workers=2) == 0
    target = layout.extracted / "Бургас-Айтос-Айтос-00151"
    assert (target / "00151 ПИ.xlsx").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert run_extract(layout, workers=2) == 0  # idempotent

    assert run_ownership(layout, workers=1) == 0
    out = (layout.ownership / "ownership-parcels.tsv").read_text(encoding="utf-8")
    assert out == "Идентификатор\tСобственик и дял\n00151.1.1\tИван Петров 1/2\n"


def test_time_window():
    start, end = parse_window("22:00-06:00")
    assert in_window(parse_window("23:30-23:31")[0], start, end)
    assert in_window(parse_window("05:59-06:00")[0], start, end)
    assert not in_window(parse_window("12:00-13:00")[0], start, end)
