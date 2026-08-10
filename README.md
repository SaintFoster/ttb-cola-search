# TTB COLA Records Viewer

This repository publishes a searchable viewer for TTB COLA records.

The GitHub Actions workflow attempts a refresh every four hours and can also be run manually from the **Actions** tab. After one successful refresh each UTC day, later scheduled attempts skip the scrape and deployment; the extra schedule slots provide retries when GitHub Actions misses a trigger or TTB is unavailable.
