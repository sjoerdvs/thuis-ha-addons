# Thuis add-ons voor Home Assistant

Add-ons bij [Thuis](https://ems.svanschouwenburg.nl), een eigen energieplatform in Google Cloud.

| Add-on | Wat het doet |
| --- | --- |
| [Thuis P1-zender](thuis_p1_zender/DOCS.md) | Stuurt HomeWizard P1-metingen naar BigQuery, zodat de gehoste app live netvermogen toont. |

## Installeren

1. In Home Assistant: **Instellingen → Apps (Add-ons) → Winkel → ⋮ → Repositories**.
2. Voeg `https://github.com/sjoerdvs/thuis-ha-addons` toe.
3. Zoek **Thuis P1-zender**, installeer, vul de configuratie in en start.

Deze repository bevat alleen code: geen sleutels, adressen of meetdata.

## Ontwikkelen

```bash
pip install -r thuis_p1_zender/requirements.txt pytest ruff
pytest
ruff check . && ruff format --check .
```
