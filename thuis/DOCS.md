# Thuis

Thuis-EMS op je eigen Home Assistant: dezelfde app als in Google Cloud, maar met de data op de Pi.
Inloggen gaat via **Cloudflare Access**; de Cloudflared-add-on maakt Thuis bereikbaar op je eigen adres.

## Opties

| Optie | Wat |
| --- | --- |
| `cf_access_team` | Je Cloudflare Zero Trust-teamnaam: het deel vóór `.cloudflareaccess.com` |
| `cf_access_aud` | De *Application Audience (AUD) tag* van de Access-applicatie |
| `toegestane_emails` | Wie erin mag (naast de regel in Cloudflare Access) |
| `alleen_kijken` | Adressen die alleen mogen kijken |
| `extra_omgeving` | Extra instellingen als `NAAM=waarde`, bijvoorbeeld `THUIS_TUYA_BEDIENEN=1` |

Zonder `cf_access_team` en `cf_access_aud` weigert Thuis elk verzoek.

## Data

Database (`thuis-data.duckdb`) en kluis (`thuis-kluis.json`) staan in de map
`addon_configs/…_thuis`. Die map zie je via de Samba-add-on en gaat mee in elke back-up van Home
Assistant. Tijdens een back-up stopt Thuis even, zodat de database altijd heel is.

De verzamelaar draait in de add-on zelf: elk kwartier (:00, :15, :30, :45) en meteen na het koppelen.
De P1-meter leest Thuis rechtstreeks uit het thuisnetwerk.

Stappenplan voor de overstap vanuit Google Cloud: `docs/raspberry-pi.md` in de repository Thuis-EMS.
