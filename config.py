"""Central configuration for the Indian Shopify store discovery pipeline.

Everything tunable lives here so the pipeline can be re-run with different
politeness/scale settings without touching the module code.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
HTTP_CACHE = CACHE / "http"
CC_CACHE = CACHE / "cc"
OUTPUT = ROOT / "output"
SEEDS = ROOT / "seeds"

CANDIDATES_FILE = DATA / "candidates.jsonl"
STORES_FILE = DATA / "stores.jsonl"          # verified + enriched records
REJECTED_FILE = DATA / "rejected.jsonl"      # candidates that failed verification
REPORT_FILE = OUTPUT / "report.json"

# --------------------------------------------------------------------------
# HTTP behaviour
# --------------------------------------------------------------------------

# Honest primary UA; some storefronts sit behind CDNs that only let
# browser-like UAs through, so a single browser-UA retry is allowed on 403.
USER_AGENT = "Mozilla/5.0 (compatible; IndianShopifyScout/1.0; store-discovery research; +https://github.com/Shoryamishra61)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

GLOBAL_CONCURRENCY = 64          # simultaneous requests across all hosts
PER_HOST_INTERVAL = 1.0          # seconds between requests to the same host
REQUEST_TIMEOUT = 10             # seconds
MAX_RETRIES = 2                  # for 429/5xx responses
RETRY_BACKOFF = 4.0              # seconds, multiplied by attempt number

CACHE_TTL = 7 * 24 * 3600        # seconds; cached pages older than this refetch

# --------------------------------------------------------------------------
# Crawl budget per store (verify + enrichment combined)
# --------------------------------------------------------------------------

MAX_PAGES_PER_STORE = 8          # hard cap on non-cached fetches per domain
PRODUCTS_JSON_LIMIT = 100        # products fetched for category classification

# --------------------------------------------------------------------------
# Common Crawl discovery
# --------------------------------------------------------------------------

CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_CRAWLS_USED = 2               # newest N crawls swept for .in / myshopify
# data.commoncrawl.org rate-limits bursts (observed: ~2000+ requests at
# ~8-10 req/s trigger sustained 403s). The sweep therefore runs in slow
# waves: low concurrency, per-request stagger, cooldown between waves.
CC_BLOCK_CONCURRENCY = 2         # parallel byte-range fetches of index blocks
CC_BLOCK_STAGGER = 0.55          # seconds between block fetch launches
CC_WAVE_SIZE = 2000              # blocks per wave before a cooldown pause
CC_WAVE_COOLDOWN = 360           # seconds between waves
CC_FAIL_TRIP = 15                # consecutive 403s -> abort wave, cool down
CC_CLUSTER_IDX = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/cluster.idx"
CC_CDX_FILE = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/{name}"

# A URL whose path matches this is treated as "likely a Shopify storefront"
# when harvesting hosts out of the CC index (index records only - we never
# fetch these URLs at discovery time).
CC_SHOPIFY_PATH_RE = r"/cdn/shop/|/collections/|/products(\.json|/)|/policies/|/pages/|/cart$|/apps/"

# myshopify subdomains whose name matches an India-related keyword are
# prioritised; a bounded random sample of the rest is added too.
CC_MYSHP_INDIA_RE = (
    r"india|indian|bharat|desi|hind|delhi|mumbai|bombay|jaipur|udaipur|jodhpur|"
    r"surat|ahmedabad|gujarat|pune|nagar|bengal|bangalore|bengaluru|chennai|"
    r"madras|kerala|kochi|cochin|goa|punjab|sikh|kolkata|bengal|patna|lucknow|"
    r"kanpur|indore|bhopal|nagpur|hyderabad|secunderabad|vijay|madurai|"
    r"trichy|rajkot|vadodara|nashik|amritsar|ludhiana|chandigarh|assam|"
    r"saree|sari|kurta|kurti|lehenga|ethnic|silk|cotton|handloom|handicraft|"
    r"ayurveda|ayurved|veda|yoga|chai|tea|masala|spice|pickle|makhana|"
    r"khadi|jute|panchakarma|namaste|mandala|henna|mehndi|diya|pooja|puja|"
    r"dhoop|agarbatti|bidi|desii|naati|swadesi|swadeshi|vocalforlocal"
)
CC_MYSHP_RANDOM_SAMPLE = 6000    # extra non-keyword myshopify hosts sampled

# --------------------------------------------------------------------------
# Search-engine discovery (optional, heavily throttled)
# --------------------------------------------------------------------------

SEARCH_ENABLED = True
SEARCH_DELAY = 8.0               # seconds between search queries
SEARCH_MAX_QUERIES = 45          # hard cap per run
SEARCH_ENGINE = "ddg-lite"       # "ddg-lite" (fallback: none)

# --------------------------------------------------------------------------
# Verification thresholds
# --------------------------------------------------------------------------

# When /meta.json is unavailable, a store is accepted as Indian only if its
# fallback evidence score reaches this threshold. Weights are in verify.py.
INDIA_FALLBACK_THRESHOLD = 3

# The CC sweep can surface >100k candidates. Verification stops once this
# many Indian Shopify stores are confirmed (buffer above the 1,000 target
# for dedup and export losses). Raising it simply continues where the last
# run stopped - candidates are consumed in priority order.
VERIFY_TARGET = 2200
# Candidate cap: never hold more than this many pending verifications per run
VERIFY_MAX_PER_RUN = 25000

# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------

MAX_EXTRA_PAGES = 5              # contact/about/legal pages beyond homepage
EXTRA_PAGE_HINTS = (
    "contact", "about", "reach", "support", "help", "story", "team",
    "shipping", "terms", "faq", "return", "refund",
)

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

CSV_FILE = OUTPUT / "indian_shopify_stores.csv"
JSON_FILE = OUTPUT / "indian_shopify_stores.json"


def ensure_dirs() -> None:
    for p in (DATA, CACHE, HTTP_CACHE, CC_CACHE, OUTPUT, SEEDS):
        p.mkdir(parents=True, exist_ok=True)


def cache_path(url: str) -> Path:
    import hashlib
    return HTTP_CACHE / (hashlib.sha256(url.encode()).hexdigest() + ".json.gz")
