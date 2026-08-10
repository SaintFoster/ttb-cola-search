# TTB COLA Records Viewer

A searchable public viewer for TTB Certificate of Label Approval (COLA) records in whiskey-related categories.

**Live viewer:** <https://saintfoster.github.io/ttb-cola-search/>

## What it contains

- A rolling 15-year repository of eligible TTB COLA records.
- Search, date-range filtering, sortable columns, and links to TTB record details.
- The current data export in `ttb_cola_results.csv`.

The project is an independent viewer built from publicly available TTB data. It is not an official TTB service; confirm important information with the source record.

## Automated refreshes

GitHub Actions attempts a refresh every day at 20:59, 21:59, 22:59, and 23:59 UTC. Once one scheduled refresh succeeds, the remaining attempts that day skip the scrape and deployment. The extra time slots provide retry opportunities when GitHub Actions or TTB is temporarily unavailable.

Repository maintainers can also select **Run workflow** in the Actions tab to force a refresh at any time.

## Run locally

Requires Python 3.12 or later.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python ttb_cola_search.py
```

The script refreshes the CSV, rebuilds `ttb_cola_records_viewer.html`, and updates the linked TTB ID data.

## Project files

| File | Purpose |
| --- | --- |
| `ttb_cola_search.py` | Fetches, validates, deduplicates, and writes the repository data. |
| `ttb_cola_results.csv` | Current records used by the viewer. |
| `ttb_cola_records_viewer.html` | Standalone static viewer published through GitHub Pages. |
| `.github/workflows/refresh-and-deploy.yml` | Scheduled refresh and Pages deployment. |

## License

This project is released under the [Unlicense](UNLICENSE). You may use, copy, modify, publish, distribute, or sell it without asking permission.
