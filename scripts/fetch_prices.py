#!/usr/bin/env python3
"""
fetch_prices.py

Reads cards.csv (player/year/set/card_number/variant/grade, filled in
manually or by identifying photos), searches TheCardAPI's sales endpoint
for each card by title text, and appends a dated snapshot (a computed
value + recent comps) to data/price_history.json.

No separate "identify" or "catalog lookup" step needed - TheCardAPI
searches directly against real sold listings by title text, so a good
player+set+year query is all that's required.

Designed to be run on a schedule (e.g. weekly via GitHub Actions) so
price_history.json accumulates a time series per card.

Free tier note: lookback is 3 days. Each weekly run naturally captures
that week's sales, which is exactly what a weekly snapshot needs. If you
later want deeper backfills, TheCardAPI's Starter tier ($9/mo) extends
lookback to 14 days.

Usage:
    export CARD_API_KEY="your_key_here"
    python fetch_prices.py --cards ./data/cards.csv --history ./data/price_history.json
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API_BASE = "https://thecardapi.com/api/v1/market"
SALES_ENDPOINT = f"{API_BASE}/sales"


def get_api_key() -> str:
    key = os.environ.get("CARD_API_KEY")
    if not key:
        sys.exit(
            "ERROR: Set the CARD_API_KEY environment variable first.\n"
            '  export CARD_API_KEY="your_key_here"'
        )
    return key


def load_cards(cards_csv: Path) -> list:
    if not cards_csv.exists():
        sys.exit(f"ERROR: cards file not found: {cards_csv}")

    with open(cards_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cards = [row for row in reader if row.get("player", "").strip()]

    if not cards:
        sys.exit("ERROR: no cards with a player name found in the CSV")

    return cards


def build_query(card: dict) -> str:
    """Build a title-search query from the card's identifying fields."""
    parts = [
        card.get("player", "").strip(),
        card.get("year", "").strip(),
        card.get("set", "").strip(),
        card.get("variant", "").strip(),
    ]
    return " ".join(p for p in parts if p)


def parse_grade_string(grade_str: str):
    """
    Splits a grade string like "BGS 8.5" or "PSA 10" into (grader, grade).
    Returns (None, None) if blank (raw card).
    """
    grade_str = (grade_str or "").strip()
    if not grade_str:
        return None, None
    parts = grade_str.split(None, 1)
    if len(parts) < 2:
        return None, None
    return parts[0].upper(), parts[1].split()[0]  # drop trailing condition text


def search_sales(query: str, api_key: str, limit: int, grader: str = None, grade: str = None) -> list:
    headers = {"x-market-api-key": api_key}
    # Pin date_from explicitly to the free tier's 3-day lookback rather than
    # relying on undocumented default behavior. Running this script daily
    # means each card's window overlaps the prior TWO days' pulls, not just
    # one - so a single missed/failed run still gets fully caught up by the
    # next day's pull. price_history.json is the real long-term archive;
    # the API itself only ever needs to answer "what sold in the last few
    # days."
    date_from = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    params = {"q": query, "limit": limit, "sort": "date_desc", "date_from": date_from}
    if grader:
        params["grader"] = grader
    if grade:
        params["grade"] = grade
    else:
        # No grade filled in -> assume raw/ungraded card
        params["graded"] = "false"

    resp = requests.get(SALES_ENDPOINT, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_reference_tree_configs() -> list:
    """
    Fixed grade-tree shape shown as market context alongside a card's own
    value: Raw as its own group, then PSA and BGS each with a couple of
    common near-mint+ grades. Same shape for every card (raw or graded) so
    the tree is easy to scan across your whole collection - the card's own
    exact grade doesn't have to be one of these rows, it's just always the
    headline value up top.
    """
    return [
        {"group": "Raw", "items": [(None, None, "Raw")]},
        {"group": "PSA", "items": [("PSA", "10", "10"), ("PSA", "9", "9")]},
        {"group": "BGS", "items": [("BGS", "10", "10"), ("BGS", "9.5", "9.5")]},
    ]


def fetch_reference_tree(
    query: str, api_key: str, ref_limit: int,
    card_grader: str, card_grade: str,
    primary_records: list, primary_value, primary_sample_size: int, primary_comps: list,
):
    """
    Builds the grouped Raw/PSA/BGS tree, and alongside it a flat "pool" of
    every confirmed sale found across all of today's queries (primary +
    every tree cell) - raw and every grade combined. That pool is what the
    dashboard uses to compute a 30-day rolling average trend line: a
    single specific grade rarely has enough sales in any 3-day window
    (especially for vintage cards) to plot a meaningful trend on its own,
    but pooling everything gives far more data points to average over.

    If a tree cell matches the card's own exact (grader, grade) - or "Raw"
    matches an ungraded card - reuse the already-fetched primary query's
    result instead of re-querying, to save an API call.
    """
    tree = []
    pool = []

    def add_to_pool(records: list, label: str):
        for r in records:
            if r.get("price_confirmed") and r.get("price") is not None:
                pool.append({
                    "price": r["price"], "date": r.get("sale_date"),
                    "url": r.get("listing_url"), "label": label,
                })

    for group in get_reference_tree_configs():
        items = []
        for ref_grader, ref_grade, label in group["items"]:
            is_own = (ref_grader == card_grader and ref_grade == card_grade)

            if is_own:
                value = primary_value
                sample_size = primary_sample_size
                comps = primary_comps[:2]
                add_to_pool(primary_records, label)
            else:
                try:
                    records = search_sales(query, api_key, ref_limit, grader=ref_grader, grade=ref_grade)
                except requests.exceptions.RequestException:
                    records = []
                value = median_confirmed_price(records)
                sample_size = len(records)
                comps = [
                    {
                        "price": c.get("price"),
                        "date": c.get("sale_date"),
                        "title": c.get("title"),
                        "url": c.get("listing_url"),
                    }
                    for c in records[:2]
                ]
                add_to_pool(records, label)
                time.sleep(0.15)  # be polite between the extra lookups

            items.append({
                "label": label, "value": value, "sample_size": sample_size,
                "comps": comps, "is_own": is_own,
            })
        tree.append({"group": group["group"], "items": items})

    return tree, pool


def median_confirmed_price(records: list):
    """Median price across price_confirmed=true records. None if none exist."""
    prices = [r["price"] for r in records if r.get("price_confirmed") and r.get("price") is not None]
    if not prices:
        return None
    return round(statistics.median(prices), 2)


def load_history(history_path: Path) -> dict:
    if history_path.exists():
        with open(history_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def card_key(card: dict) -> str:
    """Stable key per card row, since there's no catalog UUID in this pipeline."""
    parts = [
        card.get("player", ""), card.get("year", ""), card.get("set", ""),
        card.get("card_number", ""), card.get("variant", ""), card.get("grade", ""),
    ]
    return " | ".join(p.strip() for p in parts)


def main():
    parser = argparse.ArgumentParser(description="Fetch sold comps from TheCardAPI")
    parser.add_argument("--cards", default="./data/cards.csv", help="Cards CSV path")
    parser.add_argument("--history", default="./data/price_history.json", help="History JSON path")
    parser.add_argument(
        "--limit", type=int, default=25,
        help="Max records to request per card (default 25). Lower this if your "
             "collection grows large enough to approach the free tier's 5,000 "
             "records/day cap - actual usage per card is usually well below this "
             "max, since most cards won't have this many sales in a 3-day window.",
    )
    parser.add_argument(
        "--no-references", action="store_true",
        help="Skip the market-context reference lookups (raw/other-grader comps) "
             "and only fetch each card's own exact configuration. Cuts API usage "
             "roughly in half if you want to conserve daily quota.",
    )
    parser.add_argument(
        "--ref-limit", type=int, default=10,
        help="Max records per reference lookup (default 10) - kept lower than "
             "--limit since references are just context, not the card's own value.",
    )
    args = parser.parse_args()

    api_key = get_api_key()
    cards_csv = Path(args.cards)
    history_path = Path(args.history)

    cards = load_cards(cards_csv)
    print(f"Loaded {len(cards)} cards from {cards_csv}\n")

    history = load_history(history_path)
    snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_records_used = 0

    for card in cards:
        query = build_query(card)
        grader, grade = parse_grade_string(card.get("grade", ""))

        print(f"--- {card.get('player')} ({card.get('set', '?')}) ---")
        print(f"  Query: \"{query}\"" + (f" | grader={grader} grade={grade}" if grader else " | raw/ungraded"))

        try:
            records = search_sales(query, api_key, args.limit, grader=grader, grade=grade)
        except requests.exceptions.RequestException as e:
            print(f"  ERROR: {e}\n")
            continue

        total_records_used += len(records)

        value = median_confirmed_price(records)
        comps_full = [
            {
                "price": c.get("price"),
                "date": c.get("sale_date"),
                "title": c.get("title"),
                "listing_type": c.get("listing_type"),
                "price_confirmed": c.get("price_confirmed"),
                "url": c.get("listing_url"),
            }
            for c in records
        ]
        comps = comps_full[:5]

        tree = []
        pool = []
        if not args.no_references:
            tree, pool = fetch_reference_tree(
                query, api_key, args.ref_limit, grader, grade,
                primary_records=records, primary_value=value,
                primary_sample_size=len(records), primary_comps=comps_full,
            )
            total_records_used += sum(
                item["sample_size"] for group in tree for item in group["items"] if not item["is_own"]
            )
        elif value is not None:
            # No tree fetched, but still pool the primary card's own confirmed
            # sales so the 30-day average has at least this card's own data.
            pool = [
                {"price": r["price"], "date": r.get("date"), "url": r.get("url"),
                 "label": card.get("grade") or "Raw"}
                for r in comps_full if r.get("price_confirmed") and r.get("price") is not None
            ]

        key = card_key(card)
        entry = history.setdefault(key, {"meta": {}, "snapshots": []})
        entry["meta"] = {
            "player": card.get("player", ""),
            "year": card.get("year", ""),
            "set": card.get("set", ""),
            "card_number": card.get("card_number", ""),
            "variant": card.get("variant", ""),
            "grade": card.get("grade", ""),
            "photo_filename": card.get("photo_filename", ""),
            "back_photo_filename": card.get("back_photo_filename", ""),
            "added_at": card.get("added_at", ""),
        }
        entry["snapshots"].append({
            "date": snapshot_date,
            "value": value,
            "sample_size": len(records),
            "comps": comps,
            "tree": tree,
            "pooled_sales": pool,
        })

        value_str = f"${value}" if value is not None else "no confirmed sales this window"
        print(f"  ✓ {value_str} ({len(records)} matching records found)")
        if tree:
            tree_summary = " | ".join(
                f"{group['group']}: " + ", ".join(
                    f"{item['label']}={'$' + str(item['value']) if item['value'] is not None else 'no sale'}"
                    for item in group["items"]
                )
                for group in tree
            )
            print(f"    context — {tree_summary}")
        print()

    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

    print(f"Done. Snapshot for {snapshot_date} merged into {history_path}")
    print(f"Total records used this run: {total_records_used} (free tier cap: 5,000/day)")


if __name__ == "__main__":
    main()
