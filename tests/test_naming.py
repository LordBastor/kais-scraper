from pathlib import PurePosixPath

from kais_scraper.naming import (NameRegistry, extract_dir_name, geojson_name,
                                 local_name, ownership_category)


def test_local_name_matches_bash_rules():
    assert local_name("област Бургас") == "Бургас"
    assert local_name("област Бургас/община Айтос") == "Бургас-Айтос"
    assert (local_name("област Бургас/община Айтос/гр. Айтос (00151)/00151.zip")
            == "Бургас-Айтос-Айтос-00151-00151.zip")
    assert local_name("област Бургас/община Айтос/с. Лясково (44152)") == "Бургас-Айтос-Лясково-44152"


def test_extract_dir_name():
    assert extract_dir_name("Бургас-Айтос-Айтос-00151-00151.zip") == "Бургас-Айтос-Айтос-00151"
    assert extract_dir_name("София-Столична-София-68134 - район Младост.zip") == "София-Столична-София-68134-Младост"
    assert extract_dir_name("no-code.zip") == "no-code"


def test_geojson_name():
    assert geojson_name(PurePosixPath("Айтос-00151/Parcels (Polygon).shp")) == "Айтос-00151-Parcels.geojson"
    assert geojson_name(PurePosixPath("Айтос-00151/Points.shp")) == "Айтос-00151-Points.geojson"


def test_ownership_category():
    assert ownership_category("00151 ПИ") == "parcels"
    assert ownership_category("00151 сгради") == "buildings"
    assert ownership_category("00151 СОС") == "units"
    assert ownership_category("legend") is None


def test_registry_disambiguates_collisions():
    reg = NameRegistry()
    a = reg.name_for("област X/(a).zip")
    b = reg.name_for("област X/a.zip")
    assert a == "X-a.zip" and b != a and b.endswith(".zip")
    assert reg.name_for("област X/(a).zip") == a
