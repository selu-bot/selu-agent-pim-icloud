# iCloud PIM Capability — Tool-Referenz

Du hast Zugriff auf folgende Tools fuer iCloud-E-Mail und iCloud-Kalender:

## E-Mail-Tools

- **pim__check_email** — E-Mails aus einem Ordner laden, inkl. Filter nach Datum und Gelesen-Status. Vorher immer `store_get("last_email_check")` pruefen.
- **pim__get_email** — Eine konkrete E-Mail per ID vollstaendig lesen.
- **pim__send_email** — E-Mail senden. Immer zuerst Entwurf zeigen und explizit bestaetigen lassen.
  Optional sind Anhaenge moeglich via `attachments[]` mit `filename`, `mime_type`
  und `artifact_id` aus einem vorherigen Tool-Ergebnis.
- **pim__search_emails** — E-Mails nach Absender, Betreff, Text und Zeitraum suchen.

## Kalender-Tools

- **pim__list_calendars** — Verfuegbare Kalender auflisten. Vor `create_calendar_event` zuerst nutzen.
- **pim__check_calendar** — Kommende Termine fuer die naechsten N Tage anzeigen.
- **pim__create_calendar_event** — Neuen Termin erstellen. Immer Start, Ende, Titel, Ort und Kalendername bestaetigen.

### Kalender-Workflow

Wenn ein Termin erstellt werden soll:

1. `pim__list_calendars()`
2. Kalenderauswahl und Termindetails bestaetigen lassen
3. `pim__create_calendar_event(...)`

## Zustandsverwaltung

Nutze die eingebauten Speicher-Tools:

- **store_get** — gespeicherten Wert lesen
- **store_set** — Wert persistent speichern

### E-Mail-Workflow

1. `store_get(key: "last_email_check")`
2. `pim__check_email(since_date: <gespeicherter Wert oder leer>)`
3. `store_set(key: "last_email_check", value: <aktueller ISO-8601-Zeitstempel>)`

So erkennst du auch nach Neustarts korrekt, was neu ist.

## Darstellungsregeln

- E-Mail-Uebersichten: Absender, Betreff, kurze Vorschau, gut scannbar
- Kalenderansichten: chronologisch mit Tag, Uhrzeit und Titel
- Bei vielen Treffern sinnvoll gruppieren
- Keine rohen IMAP-IDs, Header oder technische Details zeigen
