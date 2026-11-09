# OpenFootball history source: Spain Segunda División

The `openfootball/football.json` repository states that the football.json schema, data, and scripts are dedicated to the public domain, and includes a [CC0 1.0 Universal license](https://github.com/openfootball/football.json/blob/0a241f6914686a1c93babe34d77af1b0b9787e81/LICENSE.md). Its README documents the JSON shape (`name`, `matches`, `date`, `team1`, `team2`, and `score.ft`) and the Spanish Segunda División coverage.

Verified source files (commit-pinned):

- [2024–25 es.2.json](https://github.com/openfootball/football.json/blob/6a225eabc8be1f7e354faa55befe790fea93332d/2024-25/es.2.json): 462 fixtures, 453 full-time score objects, dates 2024-08-15 through 2025-06-01.
- [2025–26 es.2.json](https://github.com/openfootball/football.json/blob/0d15b452ab561042631ae2f9f94ea7a5eaa0ef4a/2025-26/es.2.json): 462 fixtures, 115 `score.ft` results plus 16 two-number score arrays whose final-status meaning is not explicit; 275 rows lack a source time. The latter file is not evidence of complete season results. Array scores for past fixtures are retained by the adapter with `result_format=score_array_status_unverified`; consumers should treat this provenance as uncertain.

`app.scrapers.openfootball` only parses a JSON document supplied by its caller. It does not fetch data or modify the database. It preserves `team1`/`team2` order, uses stable `openfootball:es.2:` IDs, stores date-only `kickoff` values, and records any local source time without assigning a timezone. A cutoff excludes the entire cutoff date because source timezone/ordering cannot be proven. Unplayed rows are skipped; malformed and contradictory rows fail closed.

The repository's CC0 declaration supports reuse of that repository's dataset. It does not establish rights for similarly shaped data from other providers.

## Application integration

Opt-in import: `python -m app.model.licensed_history --file /path/es.2.json --season 2024-25`. The importer writes only its namespaced history metadata, preserves source/CC0 provenance, excludes ambiguous score-array rows from fitting, and requires at least 120 confirmed results. Use `LOCAL_DB_PATH` for isolated verification.

The small-league detail/prompt can expose a separate independent Poisson estimate when team identity, sample size, numerical convergence and recent history checks pass. It does not silently replace the market candidate or claim calibrated confidence. Current source history is incomplete/stale: the 453-result 2024/25 file yields an independent estimate for Almería–Cádiz as of 2025-06-10, but correctly returns `team_history_stale` for 2026-11-09. The current `2026-27/es.2.json` URL returned HTTP 404 during audit. Thus this integration establishes usable licensed-history modeling without inventing current coverage for Bulgarian or other unsupported leagues.
