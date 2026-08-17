"""Download football results CSVs from football-data.co.uk.

The site publishes one CSV per season/division combination at
``https://www.football-data.co.uk/mmz4281/<season>/<division>.csv``, where
``<season>`` is a 4-digit code ('2526' = the 2025/2026 season) and ``<division>``
is a league code ('E0' = the English Premier League).

Two entry points sit on top of that:

* ``download_season_division`` fetches a single season/division combination.
* ``download_from_config`` loops over every combination listed in a JSON config.

Usage:
    python download_results.py                                   # uses the England config
    python download_results.py --config config_all_combinations.json
    python download_results.py --output-dir C:/data/football
    python download_results.py --overwrite                       # re-fetch existing files
    python download_results.py --season 2526 --division E0       # ad hoc single download
"""

import argparse
import csv
import datetime
import io
import json
import os
import sys
import time

import requests

DEFAULT_CONFIG = "config_england_top4_last3_seasons.json"
DEFAULT_BASE_URL = "https://www.football-data.co.uk/mmz4281"
DEFAULT_OUTPUT_DIR = "data"
REQUEST_TIMEOUT = 60
RETRY_COUNT = 3
RETRY_DELAY_SECONDS = 2
POLITE_DELAY_SECONDS = 0.5
USER_AGENT = "Mozilla/5.0 (compatible; football-data-downloader/1.0)"


def build_url(season_code, division_code, base_url=DEFAULT_BASE_URL):
    """Return the download URL for one season/division combination."""
    return "{0}/{1}/{2}.csv".format(base_url.rstrip("/"), season_code, division_code)


def build_output_path(output_dir, season_code, division_code):
    """Return the local path a season/division CSV is saved to.

    Files are grouped in a per-season folder and named after the division, so the
    archive on disk mirrors the way the site organises its own downloads.
    """
    return os.path.join(output_dir, season_code, "{0}.csv".format(division_code))


def fetch_csv(url, timeout=REQUEST_TIMEOUT, retries=RETRY_COUNT, retry_delay=RETRY_DELAY_SECONDS):
    """Fetch a CSV over HTTP and return its raw bytes.

    The site runs Apache with MultiViews, so a URL for a file it does not have
    is *not* a clean 404. It can come back as a 300 (Multiple Choices) HTML page,
    or as a 200 carrying a different division's CSV. Anything that is not a 200
    serving CSV is therefore rejected here, and the caller additionally checks
    that the payload holds the division that was actually asked for.

    Retries on transient network errors and 5xx responses; a definitive
    "not published" answer is raised immediately rather than retried.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)

            if response.status_code in (300, 404):
                raise FileNotFoundError("not published on the site: {0}".format(url))
            response.raise_for_status()
            if response.status_code != 200:
                raise FileNotFoundError(
                    "unexpected HTTP {0} for {1}".format(response.status_code, url)
                )

            content_type = response.headers.get("Content-Type", "")
            if "html" in content_type.lower():
                raise FileNotFoundError(
                    "server returned a web page rather than a CSV: {0}".format(url)
                )

            return response.content
        except FileNotFoundError:
            raise
        except requests.RequestException as error:
            last_error = error
            if attempt < retries:
                time.sleep(retry_delay * attempt)
    raise IOError("failed after {0} attempts: {1} ({2})".format(retries, url, last_error))


def decode_csv(content):
    """Decode CSV bytes to text, stripping the UTF-8 BOM the newer files carry."""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Older seasons contain latin-1 accented characters in team/referee names.
        return content.decode("latin-1")


def season_start_year(season_code):
    """Convert a 4-digit season code into its starting calendar year.

    The archive starts in 1993/94, so a leading pair of 93 or higher is a
    1900s season and anything lower is a 2000s season ('9394' -> 1993,
    '0001' -> 2000, '2526' -> 2025).
    """
    first_pair = int(season_code[:2])
    return 1900 + first_pair if first_pair >= 93 else 2000 + first_pair


def parse_match_date(raw_date):
    """Parse a match date, which is dd/mm/yy in older files and dd/mm/yyyy in newer ones."""
    text = (raw_date or "").strip()
    for date_format in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def read_rows(content):
    """Parse CSV bytes into a list of dict rows, ignoring the trailing blank lines."""
    reader = csv.DictReader(io.StringIO(decode_csv(content)))
    return [row for row in reader if (row.get("Div") or "").strip()]


def read_divisions(rows):
    """Return the set of values in the parsed rows' ``Div`` column.

    Every file in the archive, back to 1993/94, carries ``Div`` as its first
    column, which makes it the reliable way to confirm the payload really is the
    division that was requested.
    """
    return {row["Div"].strip() for row in rows}


def season_matches_dates(rows, season_code, tolerance=0.9):
    """Return True if the rows' match dates fall inside the given season.

    European league seasons run roughly August to May, so every match should sit
    between 1 July of the starting year and 31 August of the next. A small
    tolerance absorbs the occasional malformed date without letting an entirely
    different season through.
    """
    dates = [date for date in (parse_match_date(row.get("Date")) for row in rows) if date]
    if not dates:
        return False
    start_year = season_start_year(season_code)
    window_start = datetime.date(start_year, 7, 1)
    window_end = datetime.date(start_year + 1, 8, 31)
    inside = sum(1 for date in dates if window_start <= date <= window_end)
    return inside >= tolerance * len(dates)


def validate_csv(content, division_code, season_code):
    """Raise ValueError unless the payload really is the requested season/division.

    Guards against the site's MultiViews behaviour, which answers a request for a
    file it does not hold with a *different* file rather than an error: asking for
    a season it has no folder for can return another season's data for the same
    division, and asking for a division it lacks can return another division.
    """
    rows = read_rows(content)
    if not rows:
        raise ValueError("CSV contains no match rows")

    divisions = read_divisions(rows)
    if divisions != {division_code}:
        raise ValueError(
            "CSV holds division {0}, not {1} (the site served a different file)".format(
                "/".join(sorted(divisions)), division_code
            )
        )

    if not season_matches_dates(rows, season_code):
        dates = [date for date in (parse_match_date(row.get("Date")) for row in rows) if date]
        span = "{0} to {1}".format(min(dates), max(dates)) if dates else "unparseable dates"
        raise ValueError(
            "CSV covers {0}, which is not season {1} (the site served a different file)".format(
                span, season_code
            )
        )


def save_bytes(content, output_path):
    """Write bytes to disk, creating the parent directory if needed."""
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "wb") as handle:
        handle.write(content)


def download_season_division(
    season_code,
    division_code,
    output_dir=DEFAULT_OUTPUT_DIR,
    base_url=DEFAULT_BASE_URL,
    overwrite=False,
):
    """Download the results CSV for one season/division combination.

    Args:
        season_code: 4-digit season code, e.g. '2526' for 2025/2026.
        division_code: league code, e.g. 'E0', 'E1', 'E2', 'E3'.
        output_dir: root folder the CSV tree is written under.
        base_url: archive root; overridable for testing.
        overwrite: re-download even when the file already exists.

    Returns:
        A result dict with keys ``season_code``, ``division_code``, ``url``,
        ``path``, ``status`` ('downloaded', 'skipped' or 'failed'), ``bytes``
        and ``error``.
    """
    url = build_url(season_code, division_code, base_url)
    output_path = build_output_path(output_dir, season_code, division_code)
    result = {
        "season_code": season_code,
        "division_code": division_code,
        "url": url,
        "path": output_path,
        "status": "failed",
        "bytes": 0,
        "error": None,
    }

    if os.path.exists(output_path) and not overwrite:
        result["status"] = "skipped"
        result["bytes"] = os.path.getsize(output_path)
        return result

    try:
        content = fetch_csv(url)
        # Validate before writing, so a bad response never lands on disk.
        validate_csv(content, division_code, season_code)
    except (FileNotFoundError, IOError, ValueError) as error:
        result["error"] = str(error)
        return result

    save_bytes(content, output_path)
    result["status"] = "downloaded"
    result["bytes"] = len(content)
    return result


def load_config(config_path):
    """Read a download config from a JSON file."""
    with open(config_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def describe_entry(entry):
    """Build a readable label for one config entry, for progress output."""
    parts = [entry.get("country"), entry.get("division_name"), entry.get("season_label")]
    known = [part for part in parts if part]
    if known:
        return " ".join(known)
    return "{0} {1}".format(entry.get("season_code", "?"), entry.get("division_code", "?"))


def download_from_config(
    config,
    output_dir=None,
    overwrite=False,
    polite_delay=POLITE_DELAY_SECONDS,
    progress=True,
):
    """Download every season/division combination listed in a config.

    Args:
        config: parsed config dict with a ``downloads`` list.
        output_dir: overrides the config's own ``output_dir`` when given.
        overwrite: re-download files that already exist.
        polite_delay: seconds to pause between requests, to go easy on the site.
        progress: print a line per combination as it is processed.

    Returns:
        The list of result dicts from ``download_season_division``.
    """
    base_url = config.get("base_url", DEFAULT_BASE_URL)
    target_dir = output_dir or config.get("output_dir", DEFAULT_OUTPUT_DIR)
    entries = config.get("downloads", [])

    results = []
    for index, entry in enumerate(entries, start=1):
        result = download_season_division(
            entry["season_code"],
            entry["division_code"],
            output_dir=target_dir,
            base_url=base_url,
            overwrite=overwrite,
        )
        result["label"] = describe_entry(entry)
        results.append(result)

        if progress:
            print(
                "[{0}/{1}] {2:<10} {3:<45} {4}".format(
                    index,
                    len(entries),
                    result["status"],
                    result["label"],
                    result["error"] or result["path"],
                )
            )

        # Only pause after a request that actually hit the network.
        if polite_delay and result["status"] != "skipped" and index < len(entries):
            time.sleep(polite_delay)

    return results


def summarise(results):
    """Return counts per status plus the total bytes written."""
    summary = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
    for result in results:
        summary[result["status"]] += 1
        if result["status"] == "downloaded":
            summary["bytes"] += result["bytes"]
    return summary


def print_summary(results):
    """Print the run summary, listing any failures."""
    summary = summarise(results)
    print(
        "\n{0} downloaded, {1} skipped (already present), {2} failed "
        "({3:.1f} MB written).".format(
            summary["downloaded"],
            summary["skipped"],
            summary["failed"],
            summary["bytes"] / (1024 * 1024),
        )
    )
    failures = [result for result in results if result["status"] == "failed"]
    if failures:
        print("\nFailures:")
        for failure in failures:
            print("  {0}: {1}".format(failure.get("label", failure["url"]), failure["error"]))


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="JSON config listing the combinations to download (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="where to write the CSVs (default: the config's output_dir)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-download combinations whose CSV is already on disk",
    )
    parser.add_argument(
        "--season",
        default=None,
        help="download a single season code (e.g. 2526) instead of using the config",
    )
    parser.add_argument(
        "--division",
        default=None,
        help="division code to pair with --season (e.g. E0)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=POLITE_DELAY_SECONDS,
        help="seconds to pause between downloads (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    # Ad hoc single download: --season and --division bypass the config entirely.
    if args.season or args.division:
        if not (args.season and args.division):
            print("--season and --division must be given together.", file=sys.stderr)
            return 2
        result = download_season_division(
            args.season,
            args.division,
            output_dir=args.output_dir or DEFAULT_OUTPUT_DIR,
            overwrite=args.overwrite,
        )
        print("{0}: {1}".format(result["status"], result["error"] or result["path"]))
        return 0 if result["status"] != "failed" else 1

    if not os.path.exists(args.config):
        print("Config not found: {0}".format(args.config), file=sys.stderr)
        return 2

    config = load_config(args.config)
    print(
        "{0}\n{1} combination(s) to download.\n".format(
            config.get("description", args.config), len(config.get("downloads", []))
        )
    )

    results = download_from_config(
        config,
        output_dir=args.output_dir,
        overwrite=args.overwrite,
        polite_delay=args.delay,
    )
    print_summary(results)

    return 1 if any(result["status"] == "failed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
