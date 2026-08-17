"""Build a JSON config listing every season/division CSV in the football-data.co.uk archive.

The site publishes its main archive as one CSV per season/division combination at
``https://www.football-data.co.uk/mmz4281/<season>/<division>.csv``.  Each country
has a landing page that links to every one of its files, with the division name as
the link text, so the config below is scraped rather than hand-written and can be
regenerated whenever the site adds a season.

Excel (.xlsx/.xls) downloads and the "extra leagues" whole-history files under
``/new/`` are deliberately ignored: neither is a season/division CSV.

Usage:
    python build_config.py                       # writes config_all_combinations.json
    python build_config.py --output other.json
"""

import argparse
import json
import re
import sys
from urllib.parse import urljoin

import requests

BASE_URL = "https://www.football-data.co.uk/"
ARCHIVE_URL = "https://www.football-data.co.uk/mmz4281"
DEFAULT_OUTPUT = "config_all_combinations.json"
REQUEST_TIMEOUT = 60
USER_AGENT = "Mozilla/5.0 (compatible; football-data-downloader/1.0)"

# Country landing pages that serve the main season/division archive.
COUNTRY_PAGES = {
    "England": "englandm.php",
    "Scotland": "scotlandm.php",
    "Germany": "germanym.php",
    "Italy": "italym.php",
    "Spain": "spainm.php",
    "France": "francem.php",
    "Netherlands": "netherlandsm.php",
    "Belgium": "belgiumm.php",
    "Portugal": "portugalm.php",
    "Turkey": "turkeym.php",
    "Greece": "greecem.php",
}

# <A HREF="mmz4281/2526/E0.csv">Premier League</A>
ARCHIVE_LINK_PATTERN = re.compile(
    r'<a\s+href="(mmz4281/(\d{4})/([^/"]+)\.csv)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_PATTERN = re.compile(r"<[^>]+>")


def fetch_page(url):
    """Return the decoded HTML of a football-data.co.uk page."""
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    # The site is served as latin-1; requests guesses ISO-8859-1 anyway, but be explicit.
    response.encoding = "latin-1"
    return response.text


def clean_link_text(raw_text):
    """Strip any nested markup out of an anchor's text and collapse whitespace."""
    return " ".join(TAG_PATTERN.sub(" ", raw_text).replace("&nbsp;", " ").split())


def season_start_year(season_code):
    """Convert a 4-digit season code into its starting calendar year.

    The archive starts in 1993/94, so a leading pair of 93 or higher is a
    1900s season and anything lower is a 2000s season ('9394' -> 1993,
    '0001' -> 2000, '2526' -> 2025).
    """
    first_pair = int(season_code[:2])
    return 1900 + first_pair if first_pair >= 93 else 2000 + first_pair


def season_label(season_code):
    """Turn '2526' into '2025/2026'."""
    start = season_start_year(season_code)
    return "{0}/{1}".format(start, start + 1)


def parse_country_page(country, html):
    """Extract every season/division CSV entry from one country's landing page."""
    entries = {}
    for path, season_code, division_code, link_text in ARCHIVE_LINK_PATTERN.findall(html):
        # A country page links only to its own divisions, but guard against the
        # nav bar or a cross-link pulling in another country's file.
        key = (season_code, division_code)
        if key in entries:
            continue
        entries[key] = {
            "country": country,
            "division_code": division_code,
            "division_name": clean_link_text(link_text),
            "season_code": season_code,
            "season_label": season_label(season_code),
            "url": urljoin(BASE_URL, path),
        }
    return list(entries.values())


def sort_key(entry):
    """Order entries by country, then chronologically, then by division."""
    return (entry["country"], season_start_year(entry["season_code"]), entry["division_code"])


def collect_all_entries(country_pages=None):
    """Scrape every country landing page and return the combined, sorted entry list."""
    country_pages = COUNTRY_PAGES if country_pages is None else country_pages
    all_entries = []
    for country, page in country_pages.items():
        html = fetch_page(urljoin(BASE_URL, page))
        entries = parse_country_page(country, html)
        print("  {0:<12} {1:>4} season/division CSVs".format(country, len(entries)))
        all_entries.extend(entries)
    return sorted(all_entries, key=sort_key)


def build_config(entries, description, output_dir="data"):
    """Wrap scraped entries in the config structure the downloader consumes."""
    return {
        "description": description,
        "base_url": ARCHIVE_URL,
        "output_dir": output_dir,
        "downloads": entries,
    }


def write_config(config, output_path):
    """Write a config dict to disk as indented JSON."""
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="path to write the config to (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    print("Scraping football-data.co.uk country pages...")
    entries = collect_all_entries()

    config = build_config(
        entries,
        description=(
            "Every season/division results CSV available from the football-data.co.uk "
            "main archive (mmz4281). Excel downloads and the whole-history 'extra league' "
            "files under /new/ are excluded."
        ),
    )
    write_config(config, args.output)

    countries = sorted({entry["country"] for entry in entries})
    seasons = sorted({entry["season_label"] for entry in entries})
    print(
        "\nWrote {0} combinations to {1} "
        "({2} countries, seasons {3} to {4}).".format(
            len(entries), args.output, len(countries), seasons[0], seasons[-1]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
