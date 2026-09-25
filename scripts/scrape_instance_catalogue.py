"""
Stage 1 of data collection: web scraping of the public cloud instance catalogue.

Source: https://instances.vantage.sh/ (publicly accessible AWS EC2 instance comparison page).
The table is rendered client-side by JavaScript and virtualised (only rows inside the
viewport exist in the DOM), so the page is rendered in headless Chrome with a very tall
viewport, the rendered DOM is dumped, and the <table> is parsed with BeautifulSoup.

Output: ../data/raw/scraped_instance_catalogue.csv  (values kept exactly as displayed on the page)
"""

import os
import shlex
import subprocess
from datetime import datetime, timezone

import pandas as pd
from bs4 import BeautifulSoup

URL = "https://instances.vantage.sh/"
CHROME_BIN = os.environ.get("CHROME_BIN", "google-chrome")
RAW_HTML = "../data/raw/instances_vantage_rendered.html"
SCREENSHOT = "../data/raw/instances_vantage_screenshot.png"
OUT_CSV = "../data/raw/scraped_instance_catalogue.csv"

COLUMNS = [
    "instance_name", "api_name", "coremark_score", "compute_family", "ffmpeg_fps",
    "instance_memory", "vcpus", "instance_storage", "network_performance",
    "linux_on_demand", "linux_reserved", "linux_spot_min", "windows_on_demand", "windows_reserved",
]


def chrome(*args):
    cmd = shlex.split(CHROME_BIN) + ["--headless=new", "--disable-gpu", "--no-sandbox", *args, URL]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def render_page():
    result = chrome("--window-size=1600,60000", "--virtual-time-budget=30000", "--dump-dom")
    os.makedirs(os.path.dirname(RAW_HTML), exist_ok=True)
    with open(RAW_HTML, "w", encoding="utf-8") as f:
        f.write(result.stdout)
    chrome("--window-size=1600,1000", "--virtual-time-budget=15000", f"--screenshot={os.path.abspath(SCREENSHOT)}")
    return result.stdout


def parse_table(html):
    table = BeautifulSoup(html, "html.parser").find("table")
    records = []
    for row in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        if len(cells) < len(COLUMNS) or not cells[1]:
            continue  # header row / empty spacer rows of the virtualised table
        records.append(dict(zip(COLUMNS, cells[: len(COLUMNS)])))
    return pd.DataFrame(records)


def main():
    html = render_page()
    df = parse_table(html)
    df = df.drop_duplicates(subset="api_name").reset_index(drop=True)
    df["scraped_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    df.to_csv(OUT_CSV, index=False)
    print(f"Scraped {len(df):,} instance types x {df.shape[1]} attributes -> {OUT_CSV}")
    print(df.head())


if __name__ == "__main__":
    main()
