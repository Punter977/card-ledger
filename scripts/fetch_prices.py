#!/usr/bin/env python3
"""
fetch_prices.py

Reads cards.csv (player/year/set/card_number/variant/grade, filled in
manually or by identifying photos), makes ONE broad TheCardAPI search per
card (raw + every grade mixed together, no grade filter), and appends a
dated snapshot (a computed value + a dynamic grade-breakdown tree + recent
comps) to data/price_history.json.

Why one broad query instead of several targeted ones: earlier versions of
this script queried a fixed set of cells (Raw, PSA 10, PSA 9, BGS 10,
BGS 9.5) regardless of what actually sells for a given card - so a real
PSA 8 or SGC 9 sale was invisible even though it happened. Querying
broadly and then parsing each sale's *title* for grading info (most eBay
listings put it right in the title, e.g. "... PSA 9 ...") builds the tree
from whatever genuinely sold, and only shows grades that actually had
activity. It's also far cheaper: 1 API call per card instead of up to 5.

No separate "identify" or "catalog lookup" step needed - TheCardAPI
searches directly against real sold listings by title text, so a good
player+set+year query is all that's required.

Designed to be run on a schedule (e.g. daily via GitHub Actions) so
price_history.json accumulates a time series per card.

Free tier note: lookback is 3 days. Running this daily means each card's
window overlaps the prior TWO days' pulls, so a single missed run still
gets fully caught up automatically.

Usage:
    export CARD_API_KEY="your_key_here"
    python fetch_prices.py --cards ./data/cards.csv --history ./data/price_history.json
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API_BASE = "https://thecardapi.com/api/v1/market"
SALES_ENDPOINT = f"{API_BASE}/sales"

# Matches common grading company names + a grade value inside a listing
# title, e.g. "PSA 9", "BGS 9.5", "SGC 10". Deliberately conservative
# (whole-word company match) to avoid false positives on unrelated text.
GRADE_PATTERN = re.compile(r"\b(PSA|BGS|SGC|CGC|HGA|GMA|ISA)\s*(\d{1,2}(?:\.5)?)\b", re.IGNORECASE)


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
    """Build a title-search query from the card's identifying fields.

    Card number is included deliberately: without it, a query like
    "Michael Jordan 1993-94 Fleer Ultra Scoring Kings" can match ANY card
    number within a multi-player insert subset (Scoring Kings spans many
    players/numbers), or a full boxed-set listing, pulling unrelated sales
    into this card's pool and skewing its 30-day average.
    """
    parts = [
        card.get("player", "").strip(),
        card.get("year", "").strip(),
        card.get("set", "").strip(),
        f"#{card['card_number'].strip()}" if card.get("card_number", "").strip() else "",
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


def search_sales_broad(query: str, api_key: str, limit: int) -> list:
    """
    One unfiltered search: no grader/grade/graded params at all, so
    whatever mix of raw and graded listings TheCardAPI has shows up
    together, sorted most-recent-first. This is what makes the dynamic
    tree possible - we see everything that actually sold, not just a
    handful of grades we guessed to ask about.
    """
    headers = {"x-market-api-key": api_key}
    # Pin date_from explicitly to the free tier's 3-day lookback rather
    # than relying on undocumented default behavior.
    date_from = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    params = {"q": query, "limit": limit, "sort": "date_desc", "date_from": date_from}

    resp = requests.get(SALES_ENDPOINT, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def parse_grade_from_title(title: str):
    """Returns (company, grade_value) if the title mentions a grade, else (None, None) meaning raw."""
    match = GRADE_PATTERN.search(title or "")
    if match:
        return match.group(1).upper(), match.group(2)
    return None, None


def title_matches_card_number(title: str, card_number: str) -> bool:
    """
    Checks whether a sale's title actually mentions this card's number,
    as a real safeguard against fuzzy full-text search matching a
    DIFFERENT card that just shares most of the same words. This matters
    a lot for products with several insert subsets under one umbrella
    name (e.g. "1993-94 Fleer Ultra" covers the base set AND separate
    inserts like "Inside Outside" or "Scoring Kings", each with their own
    independent numbering) - without this check, a search for "Michael
    Jordan 1993-94 Fleer Ultra #30" can still return a completely
    different card (e.g. "Inside Outside #4") if enough of the other
    words overlap, since the underlying search is fuzzy, not an exact
    field match.

    If card_number is blank, no filtering is applied (nothing to check
    against). Numeric card numbers are matched as a whole word (so "30"
    doesn't accidentally match inside "130"); alphanumeric codes (e.g.
    "BL-MJ") are matched as a case-insensitive substring with boundaries.
    """
    cn = (card_number or "").strip()
    if not cn:
        return True
    title = title or ""
    if re.match(r"^\d+$", cn):
        pattern = rf"(?<!\d)#?{re.escape(cn)}(?!\d)"
        return re.search(pattern, title) is not None
    pattern = rf"(?<![A-Za-z0-9]){re.escape(cn)}(?![A-Za-z0-9])"
    return re.search(pattern, title, re.IGNORECASE) is not None


def median_confirmed_price(records: list):
    prices = [r["price"] for r in records if r.get("price_confirmed") and r.get("price") is not None]
    if not prices:
        return None
    return round(statistics.median(prices), 2)


def build_dynamic_tree(records: list, card_grader: str, card_grade: str):
    """
    Groups confirmed sales by whatever grade their title actually shows
    (or "Raw" if none), computes a median per group, and returns a tree
    sorted Raw-first then by company/grade descending. Only groups with
    at least one real sale appear - except the card's own exact
    configuration, which always gets a row (even a "no sale" one) so you
    can always see where your specific card stands.

    Returns (tree, value, sample_size, comps) - value/sample_size/comps
    are for the card's own row specifically, used as the headline figures.
    """
    groups = {}  # "Raw" or company -> { grade_value_or_None: [records] }
    confirmed = [r for r in records if r.get("price_confirmed") and r.get("price") is not None]

    for r in confirmed:
        company, grade_val = parse_grade_from_title(r.get("title", ""))
        key = company or "Raw"
        groups.setdefault(key, {}).setdefault(grade_val, []).append(r)

    own_key = card_grader or "Raw"
    own_subkey = card_grade if card_grader else None
    groups.setdefault(own_key, {}).setdefault(own_subkey, [])  # always ensure a "yours" row

    def group_sort(k):
        return (0, "") if k == "Raw" else (1, k)

    def grade_sort(v):
        if v is None:
            return 0
        try:
            return -float(v)
        except ValueError:
            return 0

    tree = []
    own_value, own_sample_size, own_comps = None, 0, []

    for company in sorted(groups.keys(), key=group_sort):
        items = []
        for grade_val in sorted(groups[company].keys(), key=grade_sort):
            recs = groups[company][grade_val]
            value = median_confirmed_price(recs)
            label = grade_val if grade_val else "Raw"
            is_own = (company == own_key and grade_val == own_subkey)
            comps = [
                {"price": c.get("price"), "date": c.get("sale_date"), "title": c.get("title"), "url": c.get("listing_url")}
                for c in recs[:2]
            ]
            items.append({"label": label, "value": value, "sample_size": len(recs), "comps": comps, "is_own": is_own})
            if is_own:
                own_value, own_sample_size, own_comps = value, len(recs), recs
        tree.append({"group": company, "items": items})

    return tree, own_value, own_sample_size, own_comps


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
        "--limit", type=int, default=40,
        help="Max records to request per card (default 40). Since this script now "
             "makes only one query per card (not up to 5), there's more daily "
             "quota headroom than before - raised the default accordingly. Lower "
             "this if your collection grows large enough to approach the free "
             "tier's 5,000 records/day cap.",
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
        print(f"  Query: \"{query}\"" + (f" | your copy: {grader} {grade}" if grader else " | your copy: raw"))

        try:
            records = search_sales_broad(query, api_key, args.limit)
        except requests.exceptions.RequestException as e:
            print(f"  ERROR: {e}\n")
            continue

        raw_count = len(records)
        card_number = card.get("card_number", "").strip()
        if card_number:
            records = [r for r in records if title_matches_card_number(r.get("title", ""), card_number)]
            filtered_out = raw_count - len(records)
            if filtered_out:
                print(f"  (filtered out {filtered_out} result(s) whose title didn't mention #{card_number} - likely a different card in the same product line)")

        total_records_used += raw_count

        tree, value, sample_size, own_records = build_dynamic_tree(records, grader, grade)

        comps = [
            {
                "price": c.get("price"), "date": c.get("sale_date"), "title": c.get("title"),
                "listing_type": c.get("listing_type"), "price_confirmed": c.get("price_confirmed"),
                "url": c.get("listing_url"),
            }
            for c in own_records[:5]
        ]

        # Pool every confirmed sale (any grade, deduped by URL happens
        # client-side in the dashboard) for the 30-day rolling average and
        # the "Recent comps" list - this is now just every confirmed
        # record from the one broad query, title included so it displays
        # nicely alongside manually-added comps.
        pool = [
            {
                "price": r["price"], "date": r.get("sale_date"), "url": r.get("listing_url"),
                "title": r.get("title", ""),
                "label": (lambda c, g: f"{c} {g}" if c else "Raw")(*parse_grade_from_title(r.get("title", ""))),
            }
            for r in records if r.get("price_confirmed") and r.get("price") is not None
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
            "sample_size": sample_size,
            "comps": comps,
            "tree": tree,
            "pooled_sales": pool,
        })

        value_str = f"${value}" if value is not None else "no confirmed sales this window"
        print(f"  ✓ {value_str} ({sample_size} matching records for your exact config, {len(records)} total returned)")
        tree_summary = " | ".join(
            f"{group['group']}: " + ", ".join(
                f"{item['label']}={'$' + str(item['value']) if item['value'] is not None else 'no sale'}"
                for item in group["items"]
            )
            for group in tree
        )
        print(f"    breakdown — {tree_summary}")
        print()

    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

    print(f"Done. Snapshot for {snapshot_date} merged into {history_path}")
    print(f"Total records used this run: {total_records_used} (free tier cap: 5,000/day)")


if __name__ == "__main__":
    main()
