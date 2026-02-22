# svtplay-auto-dl

Script for batch-downloading videos from SVT Play using [svtplay-dl](https://svtplay-dl.se/).

## Prerequisites

- Python 3.10+
- [svtplay-dl](https://svtplay-dl.se/) installed and available in `PATH`

## svtplay-dl-category.py

Downloads all videos from a given SVT Play category page. Handles both movies (singles) and series with multiple episodes.

### Features

- Parses SVT Play category pages to discover all available content
- Downloads movies and individual series episodes via `svtplay-dl -S`
- Downloads cover images as `poster.jpg` (Jellyfin-compatible naming)
- Organizes files into `Downloads/<Category>/<Title> (<Year>)/`
- Tracks downloads across runs to avoid re-downloading moved files
- Retry logic with permanent error tracking for failing downloads
- Detects stale series and suggests marking them as complete
- Graceful stop on Ctrl+C (finishes the current download before exiting)

### Usage

```bash
# Download all movies from the default category (Filmer)
python3 svtplay-dl-category.py

# Download from a different category
python3 svtplay-dl-category.py --url https://www.svtplay.se/kategori/drama?tab=all

# Preview what would be downloaded without actually downloading
python3 svtplay-dl-category.py --dry-run

# Mark a finished series so it's never re-checked
python3 svtplay-dl-category.py --mark-complete https://www.svtplay.se/show-name

# Undo if the series gets new seasons later
python3 svtplay-dl-category.py --unmark-complete https://www.svtplay.se/show-name
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | `.../kategori/filmer?tab=all` | Category page URL |
| `--output-dir` | `Downloads` | Base output directory |
| `--seen-file` | `seen_urls.txt` | Tracks completed movies and manually-completed series |
| `--seen-episodes-file` | `seen_episodes.txt` | Tracks downloaded episode URLs |
| `--series-state-file` | `series_state.json` | Tracks series check history for staleness detection |
| `--errors-file` | `errors.json` | Tracks download errors and permanent failures |
| `--max-dl N` | `0` (no limit) | Stop after N successful downloads |
| `--sleep` | `1.0` | Delay between downloads (seconds) |
| `--stale-days` | `365` | Days without new episodes before suggesting completion |
| `--dry-run` | | Print commands without downloading |
| `--mark-complete URL` | | Add a series URL to the seen file and exit |
| `--unmark-complete URL` | | Remove a series URL from the seen file and exit |

### Tracking files

The script uses four files to maintain state across runs:

- **`seen_urls.txt`** -- Movies are added automatically after download. Series are added manually via `--mark-complete`. Any URL in this file is skipped entirely.
- **`seen_episodes.txt`** -- Individual episode URLs, added after each successful download. Since files are moved out of the download folder, this is used instead of relying on svtplay-dl's file-on-disk detection.
- **`series_state.json`** -- Per-series metadata: how many times it has been checked with no new episodes, and when the last new episode was found. Used for staleness suggestions.
- **`errors.json`** -- Per-URL error tracking. A download is retried once immediately on failure, then retried on the next run. After 3 total failed runs, the URL is marked as a permanent error and skipped with a warning.

### Output structure

```
Downloads/
  Filmer/
    Alltid nära dig (2020)/
      poster.jpg
      Alltid nära dig (2020).mkv
    Bron (2011)/
      poster.jpg
      Bron.S01E01.avsnitt-1.mkv
      Bron.S01E02.avsnitt-2.mkv
      ...
```

### Graceful stop

Press Ctrl+C once to stop after the current download finishes. Press Ctrl+C again to force quit immediately.
