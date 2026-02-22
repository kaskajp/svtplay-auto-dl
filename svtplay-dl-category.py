#!/usr/bin/env python3
"""
Download all videos from a SVT Play category page.

Features:
- Extracts program listings from SVT Play category pages (embedded JSON)
- Downloads movies and series episodes via svtplay-dl
- Tracks movies, episodes, series state, and errors across runs
- Downloads cover images (poster.jpg) for Jellyfin
- Graceful stop on Ctrl+C (finishes current download)
- Suggests marking stale series as complete

Usage examples:
  python3 svtplay-dl-category.py
  python3 svtplay-dl-category.py --url https://www.svtplay.se/kategori/serier?tab=all
  python3 svtplay-dl-category.py --dry-run
  python3 svtplay-dl-category.py --mark-complete https://www.svtplay.se/show-name
  python3 svtplay-dl-category.py --unmark-complete https://www.svtplay.se/show-name
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

DEFAULT_CATEGORY_URL = "https://www.svtplay.se/kategori/filmer?tab=all"
INFO_SEARCH_EXPR = r'<script\s+id="__NEXT_DATA__"[^>]*>({.+})</script>'

# ---------------------------------------------------------------------------
# Graceful stop
# ---------------------------------------------------------------------------

stop_requested = False
current_child: subprocess.Popen | None = None


def _signal_handler(signum, frame):
    global stop_requested
    if stop_requested:
        print("\nForce quit!", file=sys.stderr)
        if current_child is not None:
            current_child.terminate()
        sys.exit(1)
    print("\nGraceful stop requested. Finishing current download...",
          file=sys.stderr)
    stop_requested = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
}


def fetch_html(url: str, timeout: int = 30) -> str:
    req = Request(url, headers=_HTTP_HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def download_file(url: str, dest_path: str, timeout: int = 60) -> bool:
    req = Request(url, headers={
        "User-Agent": _HTTP_HEADERS["User-Agent"],
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"  WARNING: Image download failed: {e}", file=sys.stderr)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False

# ---------------------------------------------------------------------------
# SVT Play JSON extraction
# ---------------------------------------------------------------------------


def extract_page_json(html: str) -> dict | None:
    match = re.search(INFO_SEARCH_EXPR, html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _iter_urql_entries(page_json: dict):
    for entry in page_json.get("props", {}).get("urqlState", {}).values():
        if "data" in entry:
            try:
                yield json.loads(entry["data"])
            except (json.JSONDecodeError, TypeError):
                continue

# ---------------------------------------------------------------------------
# Category page parsing
# ---------------------------------------------------------------------------


def get_category_name(page_json: dict, url: str) -> str:
    for entry in _iter_urql_entries(page_json):
        for key, data in entry.items():
            if key == "categoryPage" and isinstance(data, dict):
                for field in ("heading", "name"):
                    if data.get(field):
                        return data[field]

    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "kategori":
        return parts[1].replace("-", " ").title()
    return "Unknown"


def get_category_items(page_json: dict) -> list[dict]:
    items: list[dict] = []
    for entry in _iter_urql_entries(page_json):
        for key, data in entry.items():
            if key != "categoryPage" or not isinstance(data, dict):
                continue
            for tab in data.get("lazyLoadedTabs", []):
                if tab.get("slug") != "all":
                    continue
                for module in tab.get("modules", []):
                    sel = module.get("selection")
                    if sel:
                        items.extend(sel.get("items", []))
    return items

# ---------------------------------------------------------------------------
# Detail page parsing — metadata
# ---------------------------------------------------------------------------


def _find_details(page_json: dict) -> dict | None:
    # Prefer entries that have smartStart (like svtplay-dl does)
    for entry in _iter_urql_entries(page_json):
        for key, data in entry.items():
            if (key == "detailsPageByPath"
                    and isinstance(data, dict)
                    and "smartStart" in data):
                return data
    for entry in _iter_urql_entries(page_json):
        for key, data in entry.items():
            if (key == "detailsPageByPath"
                    and isinstance(data, dict)
                    and "item" in data):
                return data
    return None


def get_video_metadata(html: str) -> tuple[str | None, str | None,
                                            str | None]:
    """Return (name, year, image_url) from a detail page."""
    page_json = extract_page_json(html)
    if not page_json:
        return None, None, None

    details = _find_details(page_json)
    if not details:
        return None, None, None

    name = _safe_get(details, "item", "parent", "name")
    if not name:
        name = _safe_get(details, "item", "name")

    year = _safe_get(details, "moreDetails", "productionYear")
    if year is not None:
        year = str(year)

    image_url = _image_from_json(details) or _image_from_html(html)
    return name, year, image_url


def _safe_get(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _image_url_from_dict(img: dict) -> str | None:
    if "id" in img and "changed" in img:
        return (
            f"https://www.svtstatic.se/image/original/default/"
            f"{img['id']}/{img['changed']}?format=auto&quality=100"
        )
    return None


def _image_from_json(details: dict) -> str | None:
    img = _safe_get(details, "item", "parent", "image", "wide")
    if isinstance(img, dict):
        return _image_url_from_dict(img)
    if isinstance(img, str) and img:
        return img

    img = _safe_get(details, "images", "wide")
    if isinstance(img, dict):
        return _image_url_from_dict(img)
    if isinstance(img, str) and img:
        return img

    return None

# ---------------------------------------------------------------------------
# Detail page parsing — cover image HTML fallback
# ---------------------------------------------------------------------------


class _ImageSrcsetExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_container = False
        self.image_url: str | None = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "div" and attr.get("data-css-selector") == "imageContainer":
            self._in_container = True
        if tag == "img" and self._in_container and self.image_url is None:
            srcset = attr.get("srcset", "")
            if srcset:
                best_url, best_w = None, 0
                for part in srcset.split(","):
                    part = part.strip()
                    pieces = part.rsplit(" ", 1)
                    if len(pieces) == 2:
                        try:
                            w = int(pieces[1].rstrip("w"))
                        except ValueError:
                            continue
                        if w > best_w:
                            best_w = w
                            best_url = pieces[0]
                self.image_url = best_url or attr.get("src")
            else:
                self.image_url = attr.get("src")

    def handle_endtag(self, tag):
        if tag == "div":
            self._in_container = False


def _image_from_html(html: str) -> str | None:
    p = _ImageSrcsetExtractor()
    p.feed(html)
    return p.image_url

# ---------------------------------------------------------------------------
# Episode discovery (replaces svtplay-dl -A)
# ---------------------------------------------------------------------------


def discover_episode_urls(html: str) -> list[str]:
    page_json = extract_page_json(html)
    if not page_json:
        return []

    details = _find_details(page_json)
    if not details:
        return []

    # If this is a Single, return its own URL
    parent_type = _safe_get(details, "item", "parent", "__typename")
    if parent_type == "Single":
        path = _safe_get(details, "item", "urls", "svtplay")
        if path:
            return [urljoin("https://www.svtplay.se", path)]
        return []

    videos: list[str] = []
    for module in details.get("modules", []):
        mod_id = module.get("id", "")
        if mod_id in ("upcoming", "related") or mod_id.startswith("details"):
            continue
        if "clips" in mod_id:
            continue
        sel = module.get("selection")
        if not sel:
            continue
        for item in sel.get("items", []):
            path = _safe_get(item, "item", "urls", "svtplay")
            if path:
                full = urljoin("https://www.svtplay.se", path)
                if full not in videos:
                    videos.append(full)
    return videos

# ---------------------------------------------------------------------------
# Tracking files (seen_urls.txt / seen_episodes.txt)
# ---------------------------------------------------------------------------


def load_seen(path: str) -> set[str]:
    if not path or not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_seen(path: str, url: str) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def remove_from_seen(path: str, url: str) -> bool:
    if not path or not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    filtered = [l for l in lines if l.strip() != url]
    if len(filtered) == len(lines):
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(filtered)
    return True

# ---------------------------------------------------------------------------
# JSON state files (series_state.json / errors.json)
# ---------------------------------------------------------------------------


def load_json_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_json_state(path: str, data: dict) -> None:
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

# ---------------------------------------------------------------------------
# Error tracking
# ---------------------------------------------------------------------------


def is_permanent_error(errors: dict, url: str) -> bool:
    return errors.get(url, {}).get("permanent", False)


def record_error(errors: dict, url: str, return_code: int,
                 errors_file: str) -> None:
    entry = errors.get(url, {"fail_count": 0, "permanent": False})
    entry["fail_count"] = entry.get("fail_count", 0) + 1
    entry["last_error"] = f"svtplay-dl exited with code {return_code}"
    entry["last_failure"] = datetime.now().isoformat()
    if entry["fail_count"] > 2:
        entry["permanent"] = True
    errors[url] = entry
    save_json_state(errors_file, errors)

# ---------------------------------------------------------------------------
# Series state
# ---------------------------------------------------------------------------


def update_series_state(state: dict, show_url: str, found_new: bool,
                        show_name: str, state_file: str) -> None:
    entry = state.get(show_url, {
        "name": show_name,
        "check_count": 0,
        "last_new_episode_date": None,
    })
    entry["name"] = show_name or entry.get("name", show_url)

    if found_new:
        entry["check_count"] = 0
        entry["last_new_episode_date"] = datetime.now().isoformat()
    else:
        entry["check_count"] = entry.get("check_count", 0) + 1

    state[show_url] = entry
    save_json_state(state_file, state)


def find_stale_series(state: dict, stale_days: int):
    now = datetime.now()
    for url, entry in state.items():
        checks = entry.get("check_count", 0)
        if checks < 2:
            continue
        raw = entry.get("last_new_episode_date")
        if raw:
            try:
                days = (now - datetime.fromisoformat(raw)).days
            except ValueError:
                days = 9999
        else:
            days = 9999
        if days >= stale_days:
            yield url, entry.get("name", url), days, checks

# ---------------------------------------------------------------------------
# svtplay-dl invocation
# ---------------------------------------------------------------------------


def run_svtplay_dl(url: str, output_dir: str, dry_run: bool) -> int:
    global current_child
    cmd = ["svtplay-dl", "-S", "-o", output_dir, url]
    print(f"  >> {' '.join(cmd)}")
    if dry_run:
        return 0
    try:
        current_child = subprocess.Popen(cmd, start_new_session=True)
        rc = current_child.wait()
        current_child = None
        return rc
    except FileNotFoundError:
        current_child = None
        print("ERROR: svtplay-dl not found in PATH.", file=sys.stderr)
        return 127


def download_with_retry(url: str, output_dir: str, dry_run: bool,
                        errors: dict, errors_file: str) -> bool:
    """Attempt download with one immediate retry. Returns True on success."""
    if is_permanent_error(errors, url):
        print(f"  SKIP (permanent error): {url} — see errors.json")
        return False

    rc = run_svtplay_dl(url, output_dir, dry_run)
    if rc == 0:
        if url in errors:
            del errors[url]
            save_json_state(errors_file, errors)
        return True

    print(f"  Retrying {url} ...")
    rc = run_svtplay_dl(url, output_dir, dry_run)
    if rc == 0:
        if url in errors:
            del errors[url]
            save_json_state(errors_file, errors)
        return True

    record_error(errors, url, rc, errors_file)
    entry = errors.get(url, {})
    if entry.get("permanent"):
        print(f"  PERMANENT ERROR: {url} "
              f"(failed {entry['fail_count']} times total)",
              file=sys.stderr)
    else:
        print(f"  ERROR: {url} "
              f"(will retry next run, "
              f"{entry.get('fail_count', 0)} failures total)",
              file=sys.stderr)
    return False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip(". ")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description="Download all videos from a SVT Play category page.",
    )
    ap.add_argument(
        "--url", default=DEFAULT_CATEGORY_URL,
        help="Category page URL (default: %(default)s)",
    )
    ap.add_argument(
        "--output-dir", default="Downloads",
        help="Base output directory (default: %(default)s)",
    )
    ap.add_argument(
        "--seen-file", default="seen_urls.txt",
        help="Tracks completed movie / series URLs (default: %(default)s)",
    )
    ap.add_argument(
        "--seen-episodes-file", default="seen_episodes.txt",
        help="Tracks downloaded episode URLs (default: %(default)s)",
    )
    ap.add_argument(
        "--series-state-file", default="series_state.json",
        help="Tracks series check history (default: %(default)s)",
    )
    ap.add_argument(
        "--errors-file", default="errors.json",
        help="Tracks download errors (default: %(default)s)",
    )
    ap.add_argument(
        "--sleep", type=float, default=1.0,
        help="Delay between downloads in seconds (default: %(default)s)",
    )
    ap.add_argument(
        "--stale-days", type=int, default=365,
        help="Days w/o new episodes before suggesting completion "
             "(default: %(default)s)",
    )
    ap.add_argument(
        "--max-dl", type=int, default=0, metavar="N",
        help="Stop after N successful downloads (0 = no limit)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print commands without actually downloading",
    )
    ap.add_argument(
        "--mark-complete", metavar="URL",
        help="Mark a series URL as complete and exit",
    )
    ap.add_argument(
        "--unmark-complete", metavar="URL",
        help="Unmark a series URL as complete and exit",
    )
    args = ap.parse_args()

    # ---- mark / unmark ----
    if args.mark_complete:
        seen = load_seen(args.seen_file)
        if args.mark_complete in seen:
            print(f"Already marked as complete: {args.mark_complete}")
        else:
            append_seen(args.seen_file, args.mark_complete)
            print(f"Marked as complete: {args.mark_complete}")
        return

    if args.unmark_complete:
        if remove_from_seen(args.seen_file, args.unmark_complete):
            print(f"Unmarked (will be re-checked): {args.unmark_complete}")
        else:
            print(f"Not found in seen file: {args.unmark_complete}")
        return

    # ---- load state ----
    seen = load_seen(args.seen_file)
    seen_episodes = load_seen(args.seen_episodes_file)
    series_state = load_json_state(args.series_state_file)
    errors = load_json_state(args.errors_file)

    # ---- fetch category page ----
    print(f"Fetching category page: {args.url}")
    try:
        cat_html = fetch_html(args.url)
    except Exception as e:
        print(f"ERROR: Failed to fetch category page: {e}", file=sys.stderr)
        sys.exit(1)

    page_json = extract_page_json(cat_html)
    if not page_json:
        print("ERROR: Could not extract JSON data from category page.",
              file=sys.stderr)
        sys.exit(1)

    category_name = get_category_name(page_json, args.url)
    items = get_category_items(page_json)
    print(f"Category: {category_name}")
    print(f"Found {len(items)} items in category listing.")

    if not items:
        print("No items found. The page structure may have changed.",
              file=sys.stderr)
        sys.exit(1)

    # ---- process items ----
    stats = dict(
        movies_downloaded=0,
        episodes_downloaded=0,
        series_checked=0,
        skipped_seen=0,
        skipped_permanent=0,
        errors_this_run=0,
    )

    def dl_limit_reached():
        if args.max_dl <= 0:
            return False
        total = stats["movies_downloaded"] + stats["episodes_downloaded"]
        return total >= args.max_dl

    for idx, item_data in enumerate(items):
        if stop_requested or dl_limit_reached():
            if dl_limit_reached():
                print(f"\nReached --max-dl={args.max_dl}. Stopping.")
            else:
                print("\nStopping as requested.")
            break

        try:
            item = item_data["item"]
            url_path = item["urls"]["svtplay"]
            item_url = urljoin("https://www.svtplay.se", url_path)
            is_single = item.get("__typename") == "Single"
            name_hint = url_path.rstrip("/").rsplit("/", 1)[-1]
        except (KeyError, TypeError) as e:
            print(f"\n  WARNING: Skipping malformed item #{idx}: {e}")
            continue

        kind = "Movie" if is_single else "Series"
        print(f"\n[{idx + 1}/{len(items)}] {kind}: {name_hint}")

        if item_url in seen:
            print("  Skipped (in seen file)")
            stats["skipped_seen"] += 1
            continue

        # ---- fetch detail page ----
        print(f"  Fetching: {item_url}")
        try:
            detail_html = fetch_html(item_url)
        except Exception as e:
            print(f"  ERROR fetching detail page: {e}", file=sys.stderr)
            stats["errors_this_run"] += 1
            continue

        name, year, image_url = get_video_metadata(detail_html)
        if not name:
            name = name_hint

        folder_name = (f"{sanitize_filename(name)} ({year})"
                       if year else sanitize_filename(name))
        folder_path = os.path.join(
            args.output_dir, sanitize_filename(category_name), folder_name,
        )
        if not args.dry_run:
            os.makedirs(folder_path, exist_ok=True)
        print(f"  -> {folder_path}")

        # ---- poster image ----
        poster_path = os.path.join(folder_path, "poster.jpg")
        if image_url and not os.path.exists(poster_path):
            print("  Downloading poster...")
            if args.dry_run:
                print(f"  >> (dry-run) download poster -> {poster_path}")
            else:
                download_file(image_url, poster_path)

        # ---- download ----
        if is_single:
            if is_permanent_error(errors, item_url):
                print(f"  SKIP (permanent error) — see errors.json")
                stats["skipped_permanent"] += 1
                continue

            if download_with_retry(item_url, folder_path, args.dry_run,
                                   errors, args.errors_file):
                if not args.dry_run:
                    append_seen(args.seen_file, item_url)
                seen.add(item_url)
                stats["movies_downloaded"] += 1
            else:
                stats["errors_this_run"] += 1

        else:
            stats["series_checked"] += 1
            episode_urls = discover_episode_urls(detail_html)
            total_eps = len(episode_urls)

            new_eps = [
                ep for ep in episode_urls
                if ep not in seen_episodes
                and not is_permanent_error(errors, ep)
            ]
            perm_skipped = sum(
                1 for ep in episode_urls
                if is_permanent_error(errors, ep)
            )
            if perm_skipped:
                stats["skipped_permanent"] += perm_skipped

            print(f"  Episodes: {total_eps} total, {len(new_eps)} new"
                  + (f", {perm_skipped} permanently failed"
                     if perm_skipped else ""))

            found_new = len(new_eps) > 0

            for ep_i, ep_url in enumerate(new_eps):
                if stop_requested or dl_limit_reached():
                    break
                print(f"  Episode [{ep_i + 1}/{len(new_eps)}]: {ep_url}")
                if download_with_retry(ep_url, folder_path, args.dry_run,
                                       errors, args.errors_file):
                    if not args.dry_run:
                        append_seen(args.seen_episodes_file, ep_url)
                    seen_episodes.add(ep_url)
                    stats["episodes_downloaded"] += 1
                else:
                    stats["errors_this_run"] += 1

                if (args.sleep > 0
                        and ep_i < len(new_eps) - 1
                        and not stop_requested):
                    time.sleep(args.sleep)

            if not args.dry_run:
                update_series_state(series_state, item_url, found_new,
                                    name, args.series_state_file)

        if args.sleep > 0 and idx < len(items) - 1 and not stop_requested:
            time.sleep(args.sleep)

    # ---- stale series suggestions ----
    stale = list(find_stale_series(series_state, args.stale_days))
    stale = [(u, n, d, c) for u, n, d, c in stale if u not in seen]
    if stale:
        print(f"\n{'=' * 60}")
        print("STALE SERIES — consider marking as complete:")
        print(f"{'=' * 60}")
        for url, name, days, checks in stale:
            print(f'\n  "{name}" — no new episodes for {days} days '
                  f"(checked {checks} times)")
            print(f"    python3 {sys.argv[0]} --mark-complete {url}")

    # ---- summary ----
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Movies downloaded:       {stats['movies_downloaded']}")
    print(f"  Episodes downloaded:     {stats['episodes_downloaded']}")
    print(f"  Series checked:          {stats['series_checked']}")
    print(f"  Skipped (already seen):  {stats['skipped_seen']}")
    print(f"  Skipped (perm. error):   {stats['skipped_permanent']}")
    print(f"  Errors this run:         {stats['errors_this_run']}")
    if stop_requested:
        print("  (Run was interrupted by user)")


if __name__ == "__main__":
    main()
