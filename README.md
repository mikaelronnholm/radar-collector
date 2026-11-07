# Radar collector

Samlar varje morgon in nyheter från fasta källor (RSS/Atom, arXiv, Federal Register, Google News-sökningar) till Deeptech Radar. Ingen AI används här: posterna poängsätts med nyckelord och radarns bolagslista, och de mest relevanta hamnar i `data/top.json`, som Claudes körningar läser och lyfter in i radarn.

- `config/sources.json` – källorna. Lägg till eller ta bort fritt.
- `config/keywords.json` – nyckelord och vikter, gräns för "top" (`min_score`).
- `config/companies.json` – bolagen i radarn (uppdateras av Claude).
- `data/top.json` – veckans mest relevanta poster.
- `data/latest.json` – alla poster senaste 7 dagarna.
- `data/feeds_status.json` – vilka källor som fungerade i senaste körningen.

Körs automatiskt varje dag (fliken Actions). Starta manuellt: Actions → Collect radar news → Run workflow.
