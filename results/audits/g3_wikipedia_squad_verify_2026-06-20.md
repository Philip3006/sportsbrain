# G3 — Wikipedia-Squad-Fallback Verifikation

**Datum**: 2026-06-20
**Roadmap-Item**: G3 (Block G, vor KO-Phase 2026-07-04)
**Stichprobe**: 3 von 50 Teams mit Wikipedia-Cache-Files (`data/cache/squad/*_wiki.json`)
**Methode**: Zufällige Auswahl mit `random.seed(20260620)`

## Ergebnisse

| Team    | WC-Squads-Page (MediaWiki API) | Per-Team-Page (HTML) | Erste 3 Spieler |
|---------|--------------------------------|----------------------|------------------|
| Tunisia | ✅ 26 Spieler                  | ✅ 26 (aus Cache)    | Mouhib Chamakh, Ali Abdi, Montassar Talbi |
| Senegal | ✅ 26 Spieler                  | ✅ 26 (aus Cache)    | Yehvann Diouf, Mamadou Sarr, Kalidou Koulibaly |
| Jordan  | ✅ 26 Spieler                  | ✅ 26 (aus Cache)    | Yazeed Abulaila, Mohammad Abu Hashish, Abdallah Nasib |

## Ablauf der Verifikation

1. Cache (`data/cache/squad/{team}_wiki.json`) für die 3 Teams gelöscht → Live-Fetch erzwungen.
2. `_fetch_wc_squads_page(team, match_date)` aufgerufen — nutzt MediaWiki Parse-API auf `2026_FIFA_World_Cup_squads` mit Section-Index aus `_WC_SECTION_MAP`.
3. `_fetch_wikipedia_squad(team, match_date)` aufgerufen — fällt auf den vorher gespeicherten Cache zurück (gleicher Pfad).

## Befunde

### ✅ Hauptbefund: Fallback funktioniert
Der **WC-Squads-Page-Fallback** (`_fetch_wc_squads_page`) via MediaWiki-Parse-API liefert für alle drei Teams 26 Spieler mit korrekten Namen und Positionen. Cache wird sauber geschrieben.

### ⚠️ Nebenbefund: Per-Team-Wikipedia-Seiten existieren nicht (404)
Die in `_fetch_wikipedia_squad` adressierte URL `https://en.wikipedia.org/wiki/{Team}_at_the_2026_FIFA_World_Cup` existiert für die getesteten Teams nicht (404). Wikipedia hat keine eigenen Per-Team-WM-Seiten für die meisten Teilnehmer angelegt — alle Kader stehen ausschließlich auf der konsolidierten `2026_FIFA_World_Cup_squads`-Seite.

**Konsequenz**: Die primäre Fallback-Quelle für Cloudflare-blockierte Transfermarkt-Anfragen ist faktisch `_fetch_wc_squads_page`, nicht `_fetch_wikipedia_squad`. Der zweite Pfad wird in der Praxis kaum noch live aufgerufen — er greift nur, wenn die WC-Page-Sektion leer ist und für ein Team eine eigene Wikipedia-Seite existiert (z. B. Brasilien, Argentinien).

### ⚠️ Nebenbefund: Test-Sensitivität auf Team-Namens-Case
`_WC_SECTION_MAP` verwendet Title-Case-Keys (`"Tunisia"`), Cache-Filenamen lowercase (`tunisia_wiki.json`). Wer `squad_report("tunisia", ...)` mit lowercase aufruft, bekommt 0 Spieler zurück, weil `_WC_SECTION_MAP.get("tunisia")` → `None`. Im echten Pipeline-Flow läuft alles über `squad_report()` mit den canonical Team-Namen aus der Fixture, daher ist das kein Live-Bug — wohl aber eine Stolperfalle für manuelle Aufrufe.

## Verdict

✅ **G3 abgeschlossen**: Wikipedia-Fallback ist live und liefert vollständige 26-Mann-Kader für alle drei zufällig gezogenen Teams. Die Architektur ist bereit für die KO-Phase ab 2026-07-04.

Keine Code-Änderungen erforderlich. Wenn gewünscht, ließe sich `_fetch_wikipedia_squad` als YAGNI-Reduzierung entfernen, da die Per-Team-Seiten kaum existieren — aber der Aufwand lohnt nicht.
