# Thuis P1-zender

Leest je HomeWizard P1-meter elke 10 seconden uit via de lokale API (v2) en stuurt de metingen
elke minuut naar een BigQuery-tabel. De gehoste Thuis-app toont ze daarna als live netvermogen.

- Alleen-lezen: de zender verandert niets aan de meter.
- Valt internet weg, dan bewaart de zender de metingen (tot ±11 dagen) en stuurt ze later na.
- De HomeWizard-integratie van Home Assistant blijft gewoon werken; de meter accepteert meerdere
  gekoppelde programma's.

## Voorbereiding in Google Cloud (eenmalig)

Open Cloud Shell in je project en maak een serviceaccount dat alleen in de P1-tabel mag schrijven:

```bash
PROJECT=jouw-project-id
SA=thuis-p1-zender@$PROJECT.iam.gserviceaccount.com
gcloud iam service-accounts create thuis-p1-zender --project=$PROJECT --display-name="Thuis P1-zender (Home Assistant)"
bq add-iam-policy-binding --member="serviceAccount:$SA" --role=roles/bigquery.dataEditor "$PROJECT:thuis.homewizard_meting"
gcloud iam service-accounts keys create p1-sleutel.json --iam-account=$SA
jq -c . p1-sleutel.json
rm p1-sleutel.json
```

Kopieer de regel die `jq` toont: dat is de sleutel voor hieronder. Bewaar hem nergens anders.

## Configuratie

| Optie | Uitleg |
| --- | --- |
| IP-adres van de P1-meter | Bijvoorbeeld `192.168.1.50`. Geef de meter in je router een vast adres. |
| Sleutel van het serviceaccount | De regel uit `jq -c` hierboven. |
| Google Cloud-project | Leeg laten mag: dan komt het project uit de sleutel. |
| BigQuery-dataset en -tabel | Standaard `thuis` en `homewizard_meting`. |
| Meet- en verzendinterval | Standaard 10 en 60 seconden. |

## Eerste start: koppelen

Start de add-on en open het tabblad **Logboek**. Staat er *"Druk nu op de knop van de
P1-meter"*, druk dan binnen 5 minuten kort op de knop van de meter. Daarna staat er
*"Gekoppeld met de P1-meter"* en begint de zender te meten. Dit hoeft maar één keer.

## Problemen

| Logboek | Oplossing |
| --- | --- |
| P1-meter niet bereikbaar | Klopt het IP-adres? Staat de meter aan en in hetzelfde netwerk? |
| BigQuery weigert de sleutel | Klopt de rechtentoekenning op de tabel? Is de sleutel ingetrokken? |
| BigQuery vindt de tabel niet | Klopt het project en heeft de Thuis-verzamelaar al één ronde gedraaid? |
| Token niet meer geaccepteerd | De zender koppelt opnieuw: druk op de knop van de meter. |

Sleutel kwijt of uitgelekt? Trek hem in met
`gcloud iam service-accounts keys list --iam-account=$SA` en
`gcloud iam service-accounts keys delete SLEUTEL_ID --iam-account=$SA`, en maak een nieuwe.
