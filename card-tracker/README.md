# Card Ledger — Basketball Card Price Tracker

Free, automated tracker for ~100 basketball cards: weekly price snapshots,
recent sale comps, and a dashboard with value-over-time charts. Built on
TheCardAPI (free tier, 5,000 sales/day) + GitHub Actions + GitHub Pages.

## How it works

1. You identify each card (player, year, set, card number, grade) and put
   it in `data/cards.csv` — either by hand, or by having a photo described
   to you (e.g. by Claude) and filling the row in
2. `fetch_prices.py` searches TheCardAPI's real sold-listing data by title
   text for each card, computes a value from confirmed sales, and appends
   a dated snapshot to `data/price_history.json`
3. A GitHub Actions workflow runs step 2 automatically every day. At up to
   200 cards, one daily run comfortably fits the free tier's 5,000
   records/day cap, and daily runs give a safety margin: the 3-day
   lookback means each card's window overlaps the prior TWO days' pulls,
   so a single missed run still gets fully caught up automatically
4. `docs/index.html` reads `price_history.json` and renders the dashboard
   (value, % change, sparkline, recent comps, photo) — free hosting via
   GitHub Pages

No separate "identify" or "catalog lookup" API call is needed — TheCardAPI
searches directly against real sold listings by title text, so a good
player + set + year query is all that's required.

## One-time setup

### 1. Get a TheCardAPI key
- Sign up free at https://www.thecardapi.com — no credit card required
- Your key arrives instantly

### 2. Install dependencies
```bash
pip install requests
```

### 3. Take photos (optional, but recommended for the dashboard)
- One clear photo per card, decent lighting, card fills most of the frame
- Front is enough for plain base cards. **Also photograph the back for
  any numbered/parallel card** — the print run (e.g. "047/099") often
  only appears on the back
- To pair a front+back, name them with a shared prefix ending in
  `_front` / `_back`, e.g. `card001_front.jpg` + `card001_back.jpg`

### 4. Fill in cards.csv
Open `data/cards.csv` and add one row per card:

| Column | Example | Notes |
|---|---|---|
| `photo_filename` | card001_front.jpg | matches a file in `photos/` |
| `back_photo_filename` | card001_back.jpg | leave blank if no back photo |
| `player` | Michael Jordan | required |
| `year` | 1993-94 | season-range format works best for basketball |
| `set` | Fleer Ultra Scoring Kings | include insert/subset name if applicable |
| `card_number` | 5 | |
| `variant` | | parallel/refractor name, if any |
| `grade` | BGS 8.5 | leave blank for raw/ungraded cards |
| `notes` | | anything worth remembering |

### 5. Pull your first price snapshot
```bash
export CARD_API_KEY="your_key_here"
python scripts/fetch_prices.py --cards ./data/cards.csv --history ./data/price_history.json
```

### 6. Push to GitHub
```bash
git init
git add .
git commit -m "Initial card tracker setup"
git branch -M main
git remote add origin <your-repo-url>
git push -u origin main
```

### 7. Add your API key as a GitHub secret
Repo → Settings → Secrets and variables → Actions → New repository secret
- Name: `CARD_API_KEY`
- Value: your key

This lets the workflow (`.github/workflows/price-pull.yml`) run
automatically without exposing your key.

### 8. Turn on GitHub Pages
Repo → Settings → Pages → Source: "Deploy from a branch" → Branch: `main`,
folder: `/docs`. Your dashboard will be live at
`https://<your-username>.github.io/<repo-name>/`

## Ongoing use

- The Actions workflow runs daily automatically (edit the cron line in the
  workflow file to change the schedule)
- Bought a new card? Photo it, add a row to `cards.csv`, and the next
  scheduled run picks it up
- You can trigger a manual price pull anytime from the repo's Actions tab
  ("Card Price Pull" → "Run workflow")

## Notes

- **Free tier: 5,000 records/day, 3-day lookback.** `fetch_prices.py` pins
  `date_from` explicitly to 3 days back on every run, and the schedule
  runs daily — well within the 200-card ceiling this cap allows at the
  default `--limit 25`. Real daily usage is usually far below 5,000,
  since most cards won't have 25 matching sales in a 3-day window; the
  script prints your actual total after each run so you can watch it.
  If your collection grows past ~200 cards, lower `--limit` (e.g. 15) to
  fit more cards in the same daily budget, or upgrade to Starter ($9/mo,
  10,000 records/day + 14-day lookback).
- **"Confirmed" vs "unconfirmed" prices**: TheCardAPI marks fast-settling
  auction/best-offer prices as unconfirmed for a few minutes after close,
  then confirms them. `fetch_prices.py` only uses confirmed prices when
  computing a card's value, so a snapshot may show fewer records than
  actually matched the search.
- **Query quality matters.** The `player + year + set + variant` fields
  become a text search against real listing titles, so specific, accurate
  values in `cards.csv` produce better matches. If a card's snapshot comes
  back with 0 records, try loosening the `set` field or double-checking
  the year format.
