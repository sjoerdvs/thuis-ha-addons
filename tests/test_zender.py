import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "thuis_p1_zender"))

from p1_zender.zender import (  # noqa: E402
    BigQuery,
    Buffer,
    Verzender,
    ZenderFout,
    bewaar_koppeling,
    laad_koppeling,
    lees_opties,
    meetwaarde,
)

SLEUTEL = {"type": "service_account", "project_id": "proj-uit-sleutel", "private_key": "synthetisch"}


def _opties(tmp_path, **over):
    ruw = {
        "p1_host": "192.168.1.50",
        "service_account_json": json.dumps(SLEUTEL),
        "gcp_project": "",
        "bq_dataset": "thuis",
        "bq_tabel": "homewizard_meting",
        "meetinterval_s": 10,
        "uploadinterval_s": 60,
        **over,
    }
    pad = tmp_path / "options.json"
    pad.write_text(json.dumps(ruw), encoding="utf-8")
    return pad


def test_opties_nemen_project_uit_de_sleutel(tmp_path):
    opties = lees_opties(_opties(tmp_path))
    assert opties.project == "proj-uit-sleutel"
    assert opties.p1_host == "192.168.1.50"


def test_opties_accepteren_sleutel_zonder_regeleinden(tmp_path):
    # Het wachtwoordveld van Home Assistant haalt regeleinden weg bij plakken.
    plat = json.dumps(SLEUTEL, indent=2).replace("\n", "")
    assert lees_opties(_opties(tmp_path, service_account_json=plat)).sleutel == SLEUTEL


@pytest.mark.parametrize(
    "over",
    [
        {"p1_host": ""},
        {"service_account_json": ""},
        {"service_account_json": '{"type": "user"}'},
        {"bq_tabel": ""},
    ],
)
def test_opties_weigeren_onvolledige_instellingen(tmp_path, over):
    with pytest.raises(ZenderFout):
        lees_opties(_opties(tmp_path, **over))


def test_meetwaarde_zet_lokale_metertijd_om_naar_utc():
    meting = SimpleNamespace(
        timestamp=datetime(2026, 10, 22, 15, 0, 0),
        power_w=420,
        energy_import_kwh="1234.5",
        energy_export_kwh=10,
        meter_model="KAIFA",
        protocol_version=50,
    )
    rij = meetwaarde(meting, nu=datetime(2026, 10, 22, 13, 0, 5, tzinfo=UTC))
    assert rij["tijd"] == "2026-10-22T13:00:00+00:00"
    assert rij["power_w"] == 420.0 and rij["import_kwh"] == 1234.5 and rij["dsmr_versie"] == 50.0
    assert rij["opgehaald"] == "2026-10-22T13:00:05+00:00"


def test_meetwaarde_zonder_metertijd_gebruikt_nu_en_laat_onzin_weg():
    nu = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)
    rij = meetwaarde(SimpleNamespace(power_w=float("nan")), nu=nu)
    assert rij["tijd"] == nu.isoformat() and rij["power_w"] is None


def test_koppeling_wordt_bewaard_en_gelezen(tmp_path):
    pad = tmp_path / "p1_koppeling.json"
    assert laad_koppeling(pad) is None
    bewaar_koppeling(pad, {"token": "t", "identifier": "id"})
    assert laad_koppeling(pad) == {"token": "t", "identifier": "id"}


def test_buffer_overleeft_herstart_en_slaat_dubbele_telegrammen_over(tmp_path):
    pad = tmp_path / "buffer.jsonl"
    b = Buffer(pad)
    assert b.voeg_toe({"tijd": "a"}) and not b.voeg_toe({"tijd": "a"}) and b.voeg_toe({"tijd": "b"})
    assert [r["tijd"] for r in Buffer(pad).rijen] == ["a", "b"]


def test_buffer_houdt_alleen_de_nieuwste_rijen(tmp_path):
    b = Buffer(tmp_path / "buffer.jsonl", max_rijen=3)
    for t in "abcde":
        b.voeg_toe({"tijd": t})
    assert [r["tijd"] for r in Buffer(tmp_path / "buffer.jsonl").rijen] == ["c", "d", "e"]


class NepSessie:
    def __init__(self, *antwoorden):
        self.antwoorden = list(antwoorden)
        self.verzoeken = []

    def post(self, url, json, timeout):
        self.verzoeken.append((url, json))
        antwoord = self.antwoorden.pop(0)
        if isinstance(antwoord, Exception):
            raise antwoord
        status, body = antwoord
        return SimpleNamespace(status_code=status, ok=200 <= status < 300, json=lambda: body)


def _bq(sessie):
    return BigQuery(SLEUTEL, "proj", "thuis", "homewizard_meting", sessie=sessie)


def test_bigquery_gebruikt_meettijd_als_insert_id():
    sessie = NepSessie((200, {}))
    assert _bq(sessie).stuur([{"tijd": "t1", "power_w": 1.0}]) == set()
    url, body = sessie.verzoeken[0]
    assert url.endswith("/projects/proj/datasets/thuis/tables/homewizard_meting/insertAll")
    assert body["rows"] == [{"insertId": "t1", "json": {"tijd": "t1", "power_w": 1.0}}]


def test_bigquery_meldt_rechtenfout_duidelijk():
    with pytest.raises(ZenderFout, match="weigert de sleutel"):
        _bq(NepSessie((403, {}))).stuur([{"tijd": "t1"}])


def test_verzender_houdt_metingen_vast_tot_bigquery_weer_bereikbaar_is(tmp_path):
    buffer = Buffer(tmp_path / "buffer.jsonl")
    for t in ("t1", "t2"):
        buffer.voeg_toe({"tijd": t})
    sessie = NepSessie(ConnectionError("offline"), (200, {}))
    verzender = Verzender(_bq(sessie), buffer)

    verzender.verstuur()
    assert len(buffer) == 2 and verzender.laatste_fout

    verzender.verstuur()
    assert len(buffer) == 0 and verzender.verstuurd == 2 and verzender.laatste_fout is None
    assert len(Buffer(tmp_path / "buffer.jsonl")) == 0


def test_verzender_slaat_afgewezen_rij_over_en_verstuurt_de_rest(tmp_path):
    buffer = Buffer(tmp_path / "buffer.jsonl")
    for t in ("t1", "t2", "t3"):
        buffer.voeg_toe({"tijd": t})
    afgewezen = {
        "insertErrors": [
            {"index": 0, "errors": [{"reason": "stopped"}]},
            {"index": 1, "errors": [{"reason": "invalid"}]},
            {"index": 2, "errors": [{"reason": "stopped"}]},
        ]
    }
    sessie = NepSessie((200, afgewezen), (200, {}))
    verzender = Verzender(_bq(sessie), buffer)
    verzender.verstuur()
    assert [r["insertId"] for r in sessie.verzoeken[1][1]["rows"]] == ["t1", "t3"]
    assert len(buffer) == 0
