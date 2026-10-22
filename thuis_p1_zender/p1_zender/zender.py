"""Thuis P1-zender: leest de HomeWizard P1 lokaal uit en stuurt de metingen naar BigQuery.

Draait als Home Assistant-add-on. De instellingen staan in /data/options.json; de P1-token en
de nog niet verstuurde metingen staan ook in /data, zodat ze een herstart overleven.
Valt internet weg, dan blijven de metingen in de buffer tot BigQuery weer bereikbaar is.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

LOG = logging.getLogger("p1_zender")
DATA = Path(os.environ.get("P1_ZENDER_DATA", "/data"))
TZ = ZoneInfo("Europe/Amsterdam")
TOKEN_NAAM = "thuis-p1-zender"
KOPPEL_SECONDEN = 300
MAX_BUFFER = 100_000  # ±11 dagen bij elke 10 seconden een meting
BATCH = 500
SCOPE = "https://www.googleapis.com/auth/bigquery.insertdata"
FOUTMELDING_S = 30 * 60  # dezelfde fout hoogstens elk half uur opnieuw in het logboek
STATUS_S = 60 * 60


class ZenderFout(RuntimeError):
    pass


@dataclass(frozen=True)
class Opties:
    p1_host: str
    sleutel: dict[str, Any]
    project: str
    dataset: str
    tabel: str
    meetinterval_s: int = 10
    uploadinterval_s: int = 60


def lees_opties(pad: Path) -> Opties:
    try:
        ruw = json.loads(pad.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ZenderFout(f"De instellingen ({pad}) konden niet worden gelezen.") from err
    host = str(ruw.get("p1_host") or "").strip()
    if not host:
        raise ZenderFout("Vul bij Configuratie het IP-adres van de P1-meter in.")
    try:
        sleutel = json.loads(ruw.get("service_account_json") or "")
    except ValueError:
        sleutel = None
    if (
        not isinstance(sleutel, dict)
        or sleutel.get("type") != "service_account"
        or not sleutel.get("private_key")
    ):
        raise ZenderFout(
            "De sleutel van het serviceaccount is leeg of geen geldig JSON-sleutelbestand. "
            "Plak de volledige inhoud van het sleutelbestand bij Configuratie."
        )
    project = str(ruw.get("gcp_project") or sleutel.get("project_id") or "").strip()
    dataset = str(ruw.get("bq_dataset") or "").strip()
    tabel = str(ruw.get("bq_tabel") or "").strip()
    if not (project and dataset and tabel):
        raise ZenderFout("Vul bij Configuratie het Google Cloud-project, de dataset en de tabel in.")
    return Opties(
        p1_host=host,
        sleutel=sleutel,
        project=project,
        dataset=dataset,
        tabel=tabel,
        meetinterval_s=int(ruw.get("meetinterval_s") or 10),
        uploadinterval_s=int(ruw.get("uploadinterval_s") or 60),
    )


# ── P1-meter ──────────────────────────────────────────────────────────────────


def _getal(waarde: Any) -> float | None:
    try:
        resultaat = float(waarde)
    except (TypeError, ValueError):
        return None
    return resultaat if resultaat == resultaat and abs(resultaat) != float("inf") else None


def meetwaarde(meting: Any, nu: datetime | None = None) -> dict[str, Any]:
    """Eén rij voor homewizard_meting, in dezelfde vorm als de lokale Thuis-app."""
    nu = nu or datetime.now(UTC)
    tijd = getattr(meting, "timestamp", None)
    if not isinstance(tijd, datetime):
        tijd = nu
    elif tijd.tzinfo is None:
        tijd = tijd.replace(tzinfo=TZ)
    return {
        "tijd": tijd.astimezone(UTC).isoformat(),
        "power_w": _getal(getattr(meting, "power_w", None)),
        "import_kwh": _getal(getattr(meting, "energy_import_kwh", None)),
        "export_kwh": _getal(getattr(meting, "energy_export_kwh", None)),
        "model": getattr(meting, "meter_model", None),
        "dsmr_versie": _getal(getattr(meting, "protocol_version", None)),
        "opgehaald": nu.astimezone(UTC).isoformat(),
    }


def laad_koppeling(pad: Path) -> dict[str, str] | None:
    try:
        koppeling = json.loads(pad.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(koppeling, dict) and koppeling.get("token") and koppeling.get("identifier"):
        return koppeling
    return None


def bewaar_koppeling(pad: Path, koppeling: dict[str, str]) -> None:
    tijdelijk = pad.with_suffix(".tmp")
    tijdelijk.write_text(json.dumps(koppeling), encoding="utf-8")
    os.chmod(tijdelijk, 0o600)
    tijdelijk.replace(pad)


async def koppel(host: str) -> dict[str, str]:
    """Vraag een token aan; de meter geeft die pas na een druk op de knop."""
    from homewizard_energy import HomeWizardEnergyV2
    from homewizard_energy.errors import DisabledError

    client = HomeWizardEnergyV2(host=host, timeout=8)
    try:
        einde = time.monotonic() + KOPPEL_SECONDEN
        volgende_melding = 0.0
        token = None
        while time.monotonic() < einde:
            if time.monotonic() >= volgende_melding:
                LOG.warning("Druk nu op de knop van de P1-meter (%s) om de zender te koppelen.", host)
                volgende_melding = time.monotonic() + 30
            try:
                token = await client.get_token(TOKEN_NAAM)
                break
            except DisabledError:
                await asyncio.sleep(2)
        if not token:
            raise ZenderFout("Geen knopdruk ontvangen binnen 5 minuten.")
        device = await client.device()
        if not device.id:
            raise ZenderFout("De P1-meter gaf geen identiteit terug.")
    finally:
        await client.close()
    LOG.info("Gekoppeld met de P1-meter (%s).", getattr(device, "product_name", None) or "HomeWizard P1")
    return {"token": token, "identifier": device.id}


# ── Buffer en BigQuery ────────────────────────────────────────────────────────


class Buffer:
    """Metingen die nog niet in BigQuery staan; één JSON-regel per meting."""

    def __init__(self, pad: Path, max_rijen: int = MAX_BUFFER) -> None:
        self.pad = pad
        self.max_rijen = max_rijen
        self.rijen: list[dict[str, Any]] = []
        try:
            for regel in pad.read_text(encoding="utf-8").splitlines():
                try:
                    self.rijen.append(json.loads(regel))
                except ValueError:
                    continue
        except OSError:
            pass
        self.rijen = self.rijen[-max_rijen:]

    def __len__(self) -> int:
        return len(self.rijen)

    def voeg_toe(self, rij: dict[str, Any]) -> bool:
        if self.rijen and self.rijen[-1].get("tijd") == rij.get("tijd"):
            return False  # dezelfde P1-telegram als de vorige keer
        self.rijen.append(rij)
        if len(self.rijen) > self.max_rijen:
            del self.rijen[: len(self.rijen) - self.max_rijen]
            self._schrijf()
        else:
            with self.pad.open("a", encoding="utf-8") as bestand:
                bestand.write(json.dumps(rij) + "\n")
        return True

    def eerste(self, aantal: int) -> list[dict[str, Any]]:
        return self.rijen[:aantal]

    def vervang_begin(self, aantal: int, door: list[dict[str, Any]]) -> None:
        self.rijen[:aantal] = door
        self._schrijf()

    def _schrijf(self) -> None:
        tijdelijk = self.pad.with_suffix(".tmp")
        tijdelijk.write_text("".join(json.dumps(r) + "\n" for r in self.rijen), encoding="utf-8")
        tijdelijk.replace(self.pad)


class BigQuery:
    def __init__(
        self, sleutel: dict[str, Any], project: str, dataset: str, tabel: str, sessie: Any = None
    ) -> None:
        if sessie is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_info(sleutel, scopes=[SCOPE])
            sessie = AuthorizedSession(credentials)
        self.sessie = sessie
        self.url = (
            "https://bigquery.googleapis.com/bigquery/v2/"
            f"projects/{quote(project, safe='')}/datasets/{quote(dataset, safe='')}"
            f"/tables/{quote(tabel, safe='')}/insertAll"
        )

    def stuur(self, rijen: list[dict[str, Any]]) -> set[int]:
        """Verstuur een batch; geeft de rijen terug die BigQuery inhoudelijk afwees."""
        if not rijen:
            return set()
        body = {"rows": [{"insertId": rij["tijd"], "json": rij} for rij in rijen]}
        try:
            r = self.sessie.post(self.url, json=body, timeout=30)
        except Exception as err:
            raise ZenderFout(f"BigQuery is niet bereikbaar ({type(err).__name__}).") from err
        if r.status_code in (401, 403):
            raise ZenderFout(
                f"BigQuery weigert de sleutel (HTTP {r.status_code}); controleer het serviceaccount en de rechten op de tabel."
            )
        if r.status_code == 404:
            raise ZenderFout(
                "BigQuery vindt het project, de dataset of de tabel niet; controleer de instellingen."
            )
        if not r.ok:
            raise ZenderFout(f"BigQuery gaf HTTP {r.status_code}.")
        afgewezen = set()
        for fout in (r.json() or {}).get("insertErrors") or []:
            redenen = {e.get("reason") for e in fout.get("errors") or []}
            if redenen - {"stopped"}:
                afgewezen.add(int(fout.get("index", -1)))
        if not afgewezen and (r.json() or {}).get("insertErrors"):
            raise ZenderFout("BigQuery heeft de batch niet verwerkt; volgende keer opnieuw.")
        return afgewezen


class Verzender:
    def __init__(self, bq: BigQuery, buffer: Buffer) -> None:
        self.bq = bq
        self.buffer = buffer
        self.verstuurd = 0
        self.laatste_fout: str | None = None
        self.laatste_foutmelding = 0.0

    def verstuur(self) -> None:
        while len(self.buffer):
            batch = self.buffer.eerste(BATCH)
            try:
                afgewezen = self.bq.stuur(batch)
            except ZenderFout as err:
                self._fout(str(err))
                return
            if afgewezen:
                LOG.warning("BigQuery wees %d meting(en) af; die worden overgeslagen.", len(afgewezen))
                self.buffer.vervang_begin(len(batch), [r for i, r in enumerate(batch) if i not in afgewezen])
                continue
            self.buffer.vervang_begin(len(batch), [])
            self.verstuurd += len(batch)
            if self.laatste_fout:
                LOG.info("BigQuery is weer bereikbaar; de achterstand wordt verstuurd.")
                self.laatste_fout = None

    def _fout(self, melding: str) -> None:
        if melding != self.laatste_fout or time.monotonic() - self.laatste_foutmelding > FOUTMELDING_S:
            LOG.error("%s Nog %d meting(en) in de buffer.", melding, len(self.buffer))
            self.laatste_foutmelding = time.monotonic()
        self.laatste_fout = melding


# ── Hoofdlus ──────────────────────────────────────────────────────────────────


async def meet_en_verstuur(client: Any, opties: Opties, verzender: Verzender) -> None:
    """Loopt tot de meter de token niet meer accepteert."""
    from homewizard_energy.errors import UnauthorizedError

    volgende_upload = time.monotonic() + opties.uploadinterval_s
    volgende_status = time.monotonic() + STATUS_S
    fouten = 0
    while True:
        start = time.monotonic()
        try:
            verzender.buffer.voeg_toe(meetwaarde(await client.measurement()))
            if fouten:
                LOG.info("De P1-meter is weer bereikbaar.")
            fouten = 0
        except UnauthorizedError:
            LOG.error("De P1-meter accepteert de token niet meer; de zender koppelt opnieuw.")
            return
        except Exception as err:
            fouten += 1
            if fouten in (1, 10) or fouten % 360 == 0:
                LOG.warning(
                    "De P1-meter is niet bereikbaar (%s); controleer IP-adres en netwerk.", type(err).__name__
                )
        if time.monotonic() >= volgende_upload:
            await asyncio.to_thread(verzender.verstuur)
            volgende_upload = time.monotonic() + opties.uploadinterval_s
        if time.monotonic() >= volgende_status:
            LOG.info(
                "Status: %d meting(en) verstuurd, %d in de buffer.",
                verzender.verstuurd,
                len(verzender.buffer),
            )
            volgende_status = time.monotonic() + STATUS_S
        await asyncio.sleep(max(0.0, opties.meetinterval_s - (time.monotonic() - start)))


async def draai(opties: Opties, verzender: Verzender, data: Path = DATA) -> None:
    from homewizard_energy import HomeWizardEnergyV2

    koppelbestand = data / "p1_koppeling.json"
    while True:
        koppeling = laad_koppeling(koppelbestand)
        if koppeling is None:
            try:
                koppeling = await koppel(opties.p1_host)
            except Exception as err:
                LOG.error(
                    "Koppelen mislukt (%s: %s). Controleer het IP-adres; nieuwe poging over een minuut.",
                    type(err).__name__,
                    err,
                )
                await asyncio.sleep(60)
                continue
            bewaar_koppeling(koppelbestand, koppeling)
        client = HomeWizardEnergyV2(
            host=opties.p1_host, identifier=koppeling["identifier"], token=koppeling["token"], timeout=8
        )
        try:
            await meet_en_verstuur(client, opties, verzender)
        finally:
            await client.close()
        koppelbestand.unlink(missing_ok=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("google").setLevel(logging.WARNING)
    try:
        opties = lees_opties(DATA / "options.json")
    except ZenderFout as err:
        LOG.error("%s", err)
        return 1
    LOG.info(
        "Thuis P1-zender gestart: P1-meter %s → %s.%s.%s, elke %d s meten, elke %d s versturen.",
        opties.p1_host,
        opties.project,
        opties.dataset,
        opties.tabel,
        opties.meetinterval_s,
        opties.uploadinterval_s,
    )
    buffer = Buffer(DATA / "buffer.jsonl")
    if len(buffer):
        LOG.info("%d meting(en) uit de buffer worden nog verstuurd.", len(buffer))
    try:
        bq = BigQuery(opties.sleutel, opties.project, opties.dataset, opties.tabel)
    except Exception as err:
        LOG.error(
            "De sleutel van het serviceaccount is niet bruikbaar (%s); plak hem opnieuw.", type(err).__name__
        )
        return 1
    asyncio.run(draai(opties, Verzender(bq, buffer)))
    return 0
