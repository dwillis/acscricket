#!/usr/bin/env python3
"""
Sitemap generator for acscricket.com and subdomains.
Uses the sitemap-generator (pysitemap) library.

Usage:
    python generate_sitemaps.py                  # generate all sitemaps
    python generate_sitemaps.py acscricket.com   # generate one site only
    python generate_sitemaps.py --list           # list available site keys
    python generate_sitemaps.py -v               # verbose logging

Output files are written to ./sitemaps/ by default.
Override with OUTPUT_DIR environment variable.

Requirements:
    pip install sitemap-generator
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import aiohttp
from pysitemap.base_crawler import Crawler


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./sitemaps"))

USER_AGENT = "ACS-SitemapBot/1.0 (+https://acscricket.com; sitemap generation)"

# Each site entry:
#   root_url     - crawl starting point (must end with /)
#   out_file     - output filename within OUTPUT_DIR
#   exclude_urls - substrings; any URL containing one is skipped
#   maxtasks     - concurrent async tasks (lower = more polite to the server)
#   notes        - human-readable description

SITES = {
    "acscricket.com": {
        "root_url": "https://acscricket.com/",
        "out_file": "sitemap_acscricket.xml",
        "maxtasks": 50,
        "exclude_urls": [
            # WordPress admin & internal
            "/wp-admin/",
            "/wp-login.php",
            "/wp-includes/",
            "/wp-content/plugins/",
            "/wp-content/themes/",
            "/wp-json/",
            "/xmlrpc.php",
            "?author=",
            "/trackback/",
            "/feed/",
            "/comments/feed/",
            # Duplicate/pagination query params
            "?replytocom=",
            "?page=",
        ],
        "notes": "Main WordPress site",
    },
    "dbsearch.acscricket.com": {
        "root_url": "https://dbsearch.acscricket.com/",
        "out_file": "sitemap_dbsearch.xml",
        "maxtasks": 30,
        "exclude_urls": [
            # Dynamic search/query result URLs not useful in a sitemap
            "/search?",
            "/results?",
            "?q=",
            "?query=",
            "?s=",
        ],
        "notes": "Database search interface - dynamic query URLs excluded",
    },
    "archive.acscricket.com": {
        "root_url": "https://archive.acscricket.com/",
        "out_file": "sitemap_archive.xml",
        "maxtasks": 40,
        "exclude_urls": [
            # Exclude non-HTML assets that may surface during crawl
            ".pdf",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".zip",
            ".csv",
        ],
        "notes": "Static archive/library - all HTML pages included",
    },
    "stats.acscricket.com": {
        "root_url": "https://stats.acscricket.com/",
        "out_file": "sitemap_stats.xml",
        "maxtasks": 40,
        "exclude_urls": [
            # Exclude binary/asset URLs if they surface during crawl
            ".pdf",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".zip",
            ".csv",
        ],
        "notes": "Static records/statistics site - all HTML pages included",
    },
    "shop.acscricket.com": {
        "root_url": "https://shop.acscricket.com/",
        "out_file": "sitemap_shop.xml",
        "maxtasks": 50,
        "exclude_urls": [
            # WooCommerce transactional pages
            "/cart/",
            "/checkout/",
            "/my-account/",
            "/order-received/",
            "?add-to-cart=",
            "?wc-ajax=",
            # WordPress / WooCommerce admin & internal
            "/wp-admin/",
            "/wp-login.php",
            "/wp-includes/",
            "/wp-content/plugins/",
            "/wp-content/themes/",
            "/wp-json/",
            "/xmlrpc.php",
            "?author=",
            "/feed/",
        ],
        "notes": "WooCommerce shop - transactional and admin paths excluded",
    },
    "womenscrickethistory.org": {
        "root_url": "https://womenscrickethistory.org/",
        "out_file": "sitemap_womenscrickethistory.xml",
        "maxtasks": 40,
        "exclude_urls": [
            # Exclude binary/asset URLs if they surface during crawl
            ".pdf",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".zip",
            ".csv",
        ],
        "notes": "Static historical site - all HTML pages included",
    },
}


# ---------------------------------------------------------------------------
# Core crawler
# ---------------------------------------------------------------------------

async def _crawl(root_url, out_file, exclude_urls, maxtasks):
    """
    Instantiate and run pysitemap's Crawler inside a running event loop.
    The Crawler creates an aiohttp.ClientSession on init, so it must be
    constructed inside an async context (asyncio.run / await).
    Returns the number of successfully crawled URLs.
    """
    http_options = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": aiohttp.ClientTimeout(total=30),
    }

    crawler = Crawler(
        root_url,
        out_file=out_file,
        out_format="xml",
        maxtasks=maxtasks,
        http_request_options=http_options,
    )

    if exclude_urls:
        crawler.set_exclude_url(exclude_urls)

    await crawler.run()
    return sum(1 for v in crawler.done.values() if v)


def generate_sitemap(site_key, config, output_dir):
    """Run the crawler for a single site synchronously."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / config["out_file"]

    logging.info("=" * 60)
    logging.info("Site    : %s", site_key)
    logging.info("Notes   : %s", config["notes"])
    logging.info("Root    : %s", config["root_url"])
    logging.info("Output  : %s", out_path)
    logging.info("Exclude : %s", config["exclude_urls"] or "(none)")
    logging.info("=" * 60)

    count = asyncio.run(
        _crawl(
            root_url=config["root_url"],
            out_file=str(out_path),
            exclude_urls=config["exclude_urls"],
            maxtasks=config["maxtasks"],
        )
    )

    logging.info("Sitemap written: %s  (%d URLs)", out_path, count)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    setup_logging(verbose="--verbose" in sys.argv or "-v" in sys.argv)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if "--list" in sys.argv:
        print("\nAvailable site keys:")
        for key, cfg in SITES.items():
            print(f"  {key:<35} - {cfg['notes']}")
        print()
        return

    if args:
        unknown = [a for a in args if a not in SITES]
        if unknown:
            print(f"Unknown site key(s): {', '.join(unknown)}")
            print(f"Valid keys: {', '.join(SITES)}")
            sys.exit(1)
        targets = {k: SITES[k] for k in args}
    else:
        targets = SITES

    logging.info("Generating sitemaps for %d site(s)...", len(targets))

    errors = []
    for site_key, config in targets.items():
        try:
            generate_sitemap(site_key, config, OUTPUT_DIR)
        except Exception as exc:
            logging.error("FAILED %s: %s", site_key, exc)
            errors.append(site_key)

    print()
    if errors:
        logging.error("Completed with errors on: %s", ", ".join(errors))
        sys.exit(1)
    else:
        logging.info("All sitemaps generated successfully -> %s", OUTPUT_DIR)


if __name__ == "__main__":
    main()