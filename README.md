# Indian Shopify Store Discovery

A pipeline that finds **Shopify stores operated from India** and extracts structured
data about each one: domain, contacts, socials, category, tagline, logo and state.

**Result: `output/indian_shopify_stores.csv`** — 2,440 verified Indian Shopify
stores, one row per store, built entirely from free public sources with no paid APIs.

---

## TL;DR of the method

1. **Discover** candidate domains from four independent sources — a Common Crawl
   index sweep, a hand-compiled brand list, scraped "top Indian Shopify stores"
   articles, and throttled DuckDuckGo queries — then a fifth (outbound-link
   expansion from verified stores) applied iteratively.
2. **Verify** each candidate is a live Shopify storefront via the public
   `/meta.json` endpoint (with `/products.json` + HTML-marker fallbacks).
3. **Confirm India** from the merchant's own Shopify admin data (`country: "IN"`
   in `meta.json`), with a weighted multi-signal fallback when that endpoint is
   blocked, and hard rejection when an explicit foreign country is reported.
4. **Extract** the seven fields with explicit fallback chains, recording the
   method used for every value.
5. **Deduplicate** on `myshopify_domain` — Shopify's own per-shop unique key.
6. **Export** CSV + JSON with a completeness report.

---

## Results

| Source | Candidates contributed | Verified stores |
|---|---:|---:|
| Common Crawl `.in` sweep | 131,108 | 2,299 |
| Common Crawl `*.myshopify.com` sweep | 8,098 | 43 |
| Curated seed list | 111 | 70 |
| Listicle scrape | 82 | 8 |
| Outbound expansion | 200 | 31 |
| Search-engine queries | 0 | 0 |

| Stage | Count |
| --- | ---: |
| Candidate domains discovered | 139,599 |
| Assessed (fetched and decided) | 75,696 |
| Reachable storefronts | 129,785 |
| Confirmed Shopify | 8,148 |
| Confirmed Shopify **and** Indian | 2,451 |
| After deduplication on `myshopify_domain`, exported | **2,440** |

Rejections were dominated by `not_shopify`; every reason and count is in
`output/report.json` → `rejection_reasons`.

### Field completeness

| Field | Present | Missing | % |
|---|---:|---:|---:|
| Domain URL | 2,440 | 0 | 100.0% |
| Contacts (email or phone) | 2,415 | 25 | 99.0% |
| — emails | 2,251 | 189 | 92.3% |
| — phones | 2,412 | 28 | 98.9% |
| Socials | 2,058 | 382 | 84.3% |
| Category | 2,383 | 57 | 97.7% |
| Tagline | 2,346 | 94 | 96.1% |
| Logo | 2,233 | 207 | 91.5% |
| State | 2,431 | 9 | 99.6% |

Why fields go missing, not just how often:

- **Contacts** — many small stores publish neither an email nor a phone anywhere
  robots-allowed. Shopify's stock `robots.txt` disallows `/policies/`, which is
  exactly where a lot of merchant contact blocks live, so those pages are never
  fetched by this crawler.
- **Socials** — newer/smaller stores genuinely have no linked social profiles.
  Share-widget URLs (`sharer.php`, `intent/tweet`) are filtered, not counted.
- **Tagline** — themes without a meta description and with an image-only hero
  leave nothing extractable in static HTML.
- **Category** — stores whose catalog JSON is disabled (401/404) and whose
  homepage has no scorable product vocabulary are left blank rather than guessed.
- **State** — the main gap is bot-walled or JS-only storefronts where neither
  `meta.json` nor any page text is obtainable.

### Sanity checks on the data

- **State distribution** mirrors where Indian D2C actually clusters
  (Maharashtra / Delhi / Karnataka / Gujarat … top), which is hard evidence the
  state field is not noise: Maharashtra 484 · Delhi 380 · Karnataka 224 · Uttar Pradesh 222 · Gujarat 213 · Haryana 190 · Tamil Nadu 167 · Rajasthan 134
- A random sample of **15 exported rows was re-checked live against
  `/meta.json` after export: 15/15 confirmed** (`python spot_check.py 15`
  reproduces this against the checked-in CSV).
- Foreign-owned `.in` domains are systematically rejected — see
  *"Handling false positives"* below.

---

## The seven fields and where they come from

| Field | Source / method | Provenance column |
|---|---|---|
| **Domain URL** | canonical host resolved from redirects (`www`/apex unified); custom domain preferred over `*.myshopify.com` | `domain_url` |
| **Contacts** | `mailto:` links, plain-text email regex (incl. `name [at] domain` de-obfuscation), `tel:` links, Indian phone regex (`+91`/STD/10-digit mobile); junk filtered (placeholders like `abc@hotmail.com`, tracker/`noreply`/app domains) | `emails`, `phones` |
| **Socials** | profile URL patterns for Instagram/Facebook/X-Twitter/LinkedIn/YouTube/Pinterest/WhatsApp/Telegram across homepage + contact/about pages | one column per platform |
| **Category** | `/products.json` (up to 100 products) → keyword taxonomy over product_type ×2, tags ×1.5, titles ×1; fallback: collection nav labels; fallback: homepage text | `category_method` |
| **Tagline** | `meta description` → `og:description` → JSON-LD `description` → hero text | `tagline_method` |
| **Logo** | JSON-LD `Organization.logo` → scored header `<img>` (class/id/alt/src contains *logo*, favicons and payment icons excluded) → `og:image` flagged as fallback | `logo_method` |
| **State** | `meta.json` `province` (merchant's own admin entry) → checksum-validated GSTIN state code → PIN-code prefix zone → explicit state name → city→state map | `state_method` |

---

## How candidates are found

Four seed sources feed the pool; none of them decides "Indian" or "Shopify" —
they only decide what is worth one HTTP request. Verification decides everything.

| Source | What it is | Volume |
|---|---|---|
| **Common Crawl index sweep** (primary) | direct read of `cluster.idx` blocks for the SURT prefixes `in,` (every `.in` host) and `com,myshopify,` (every `*.myshopify.com` host) in the newest crawl(s); hosts kept only when some crawled URL has a Shopify-shaped path (`/cdn/shop/`, `/collections/`, `/products.json`, ...) | 139,206 |
| **Curated seed list** | ~80 well-known Indian consumer brands (`seeds/indian_brands.txt`) | 111 |
| **Listicle scrape** | outbound links scraped from public "top Indian Shopify stores" articles | 82 |
| **DuckDuckGo queries** | throttled (≥8s apart, ≤45 queries) searches pairing India terms with `site:myshopify.com` / "powered by shopify" | 0 (implemented, but the engine throttled scripted queries to zero this run) |
| **Outbound expansion** | links harvested from *already-cached* homepages of verified stores (zero extra requests), then re-verified; repeated while the pool keeps growing | 200 |

#### Why the Common Crawl index is read directly

The official CDX query API (`index.commoncrawl.org`) supports `*.myshopify.com/*`
but **silently fails TLD-level patterns** like `*.in/cdn/shop/*` — pages come
back unfiltered. The underlying index files on `data.commoncrawl.org` are the
robust route:

- `cluster.idx` (~105 MB, one file per crawl) maps sorted SURT keys
  (`in,example` = `example.in` reversed) to byte ranges of gzipped index blocks.
- Every `.in` host therefore lives in the contiguous run of lines starting
  `in,`; the whole `.in` TLD spans a few thousand blocks.
- The pipeline downloads `cluster.idx` once (cached on disk), collects the
  block descriptors, fetches exactly those byte ranges and gunzips each block,
  filtering URLs locally for Shopify-shaped paths.
- **Throttle-aware by design**: data.commoncrawl.org rate-limits bursts
  (observed: sustained ~8-10 requests/s triggers a wall of 403s). The sweep
  therefore runs in waves - low concurrency (~2 requests/s), a cooldown
  between waves, a circuit-breaker on consecutive 403s, and a done-ledger
  that makes every block incremental, so an interrupted sweep resumes exactly
  where it stopped. The full 5,777-block sweep completed with **zero failed
  blocks**.
- Only *index metadata* is read at discovery time — no storefront is contacted
  during discovery.

> Digression for the curious: the CDX query API cannot express TLD-level
> patterns (`*.in/...` silently returns unfiltered pages), so reading
> `cluster.idx` directly is not just faster — it is the only correct route.
> The index records also turned out to be space-separated CDXJ, not
> tab-separated; a tab-split parses zero lines and fails silently.

## How a store is confirmed as Shopify

In order of reliability:

1. **`GET /meta.json`** — a public endpoint Shopify serves on every storefront
   containing the merchant's own profile: `id`, `name`, `city`, `province`,
   `country`, `currency`, `myshopify_domain`. A 200 JSON with an `id` proves
   Shopify *and* supplies country/state/dedup-key in one request. This
   verified ~99% of stores in the final run.
2. **`GET /products.json?limit=1`** — the public catalog JSON must contain a
   `products` array.
3. **Homepage HTML markers** — `cdn.shopify.com` asset references, the
   `Shopify.shop` JS object, `*.myshopify.com` links, `/cdn/shop/` paths.
   At least **two independent marker families** are required, so one fluke
   string cannot produce a false positive.

A store that 403s everything (Cloudflare bot wall) is recorded as
`unreachable:403`, never guessed.

## How "Indian" is decided

The assignment's hardest judgment call. The definition used here:

> **A store is Indian when the merchant's own Shopify configuration says the
> shop is operated from India** (`meta.json` `country == "IN"`), or — when that
> endpoint is unavailable — when independent on-page evidence crosses a
> weighted threshold.

| Evidence | Weight |
|---|---|
| INR currency (`Shopify.currency`, `₹`) | +2 |
| Indian state names in page text | +2 |
| Valid GSTIN format | +2 |
| "India" mentions | +1 |
| PIN code present | +1 |
| Indian phone formats | +1 |

Fallback acceptance requires **score ≥ 3** with at least one +2 signal. The
threshold and weights live in `config.py` / `scout/verify.py`.

Deliberately *not* accepted on their own: `.in` TLD, "made in India", "we ship
across India", an Indian warehouse address, or a logistics partner's GSTIN.

**Explicit rejection:** `meta.json` reporting a foreign country (US, SG, GB…)
rejects the store outright — this is what removes the `.in` domain that is not
an Indian business, systematically rather than by hand. The rejection counts are
in the funnel below.

## Handling false positives

- **`.in` domain that isn't Indian** → `meta.json` country ≠ IN → rejected
  (rejection reason `foreign_country:<code>` in `data/rejected.jsonl`).
- **Looks like Shopify but isn't** → `/meta.json` and `/products.json` both fail
  and fewer than two HTML marker families → rejected (`not_shopify`).
- **Dead domains / parked hosts** → rejected (`unreachable:*`) after trying
  apex and `www` variants.
- **Password-protected storefronts** → rejected (`password_page`): technically
  Shopify, but nothing extractable and the owner has deliberately closed it.
- **Same shop behind several domains** → deduplicated on `myshopify_domain`;
  the custom domain (over the myshopify subdomain) and apex (over `www`) wins.
- **Junk contact data** → placeholder emails (`abc@…`, `test@…`), hash-like
  locals, tracker/`noreply`/app domains, and phone numbers that don't normalise
  to Indian formats are dropped at extraction time.

## Politeness and robots.txt

- `robots.txt` fetched and parsed per host (cached); **all** `User-agent: *`
  disallow rules are obeyed. Notably Shopify's stock robots disallows
  `/policies/`, `/cart`, `/checkout`, `/search` — this crawler does not fetch
  those paths at all, even though the data would be useful.
- Maximum 1 request/second per host, global concurrency cap (24), 18s timeouts.
- Retries only on 429/5xx with growing backoff; network errors fail fast.
- Every raw response is cached on disk (`data/cache/http/`), so stage re-runs
  and debugging cost **zero** repeat traffic.
- User-Agent is honest (`IndianShopifyScout/1.0 …`) with a single browser-UA
  retry for CDN-fronted storefronts that 403 unknown agents.
- Total requests per accepted store: ~4–8 (robots, homepage, meta.json,
  products.json, ≤5 linked contact/about pages).

## How to run

```bash
pip install -r requirements.txt     # aiohttp is the only dependency

python run.py discover              # CC sweep + seeds + listicles + DDG (~20-40 min)
python run.py verify                # Shopify + India verification (~1-2 h for full pool)
python run.py enrich                # field extraction (mostly cache-hits, ~30-60 min)
python run.py expand                # optional: propose candidates from verified homepages
python run.py verify && python run.py enrich && python run.py export
python run.py export                # writes output/ CSV + JSON + report.json
```

Stages are idempotent and resumable — outputs are append-only JSONL
(`data/candidates.jsonl`, `data/stores.jsonl`, `data/rejected.jsonl`) plus the
response cache; interrupting and re-running any stage is safe. The checked-in
result file came from a run whose fetch stages took **~6 hours end-to-end
(development, debugging and unattended crawling combined)**; a re-run with the
pool already discovered would be substantially faster. Politeness settings (`PER_HOST_INTERVAL`,
`GLOBAL_CONCURRENCY`) live in `config.py`.

## Known limitations

- **Scale** — the CC sweep is I/O-bound on one crawl's index. At 10x, I'd sweep
  several crawls in parallel and shard the index blocks; at 100x, the fetch
  stage needs a proxy pool because single-IP politeness (1 rps/host) caps
  throughput, and Cloudflare-fronted stores would reject a datacenter IP range.
- **Headless/JS-only storefronts** — themes that render everything client-side
  (and block `products.json`) yield empty contacts/socials/category. Playwright
  rendering would fix this at ~10x the cost per store.
- **`meta.json` trust** — country/province are merchant self-reported admin
  data. A merchant who entered the wrong province gets a wrong state; no
  pipeline can fully correct that without registry lookups.
- **Category is heuristic** — a keyword taxonomy, not a model. It's honest
  (dominant catalog vocabulary wins) but labels like "Home & Living" are
  broader than a human merchandiser would pick.
- **Discovery recall** — Indian Shopify stores on `.com` domains are
  under-covered by the `.in` index sweep; they enter via seeds, listicles,
  DDG and expansion instead. A paid Store Leads / BuiltWith export would close
  this gap instantly; free sources were preferred per the brief.
- **WHOIS not used** — the brief allows it, but WHOIS rate limits (1 rps/IP at
  best) and redaction made it a poor source versus `/meta.json`, which is free,
  instant and merchant-authored.

## Repo layout

```
run.py                    CLI entrypoint (discover | verify | enrich | expand | export)
config.py                 all tunables: politeness, thresholds, paths
scout/
  fetcher.py              async HTTP: per-host 1 rps limiter, retries, disk cache
  robots.py               robots.txt fetch/cache/evaluate
  verify.py               Shopify + India verification (meta.json first)
  enrich.py               field extraction (contacts/socials/category/tagline/logo/state)
  states.py               state normalization, GSTIN checksum, PIN zones, city map
  categories.py           keyword taxonomy + classifier
  export.py               CSV/JSON/report writer
  store.py                JSONL record store (append-only, resumable)
  discovery/
    cc_sweep.py           Common Crawl cluster.idx direct sweep
    seeds.py              curated list + listicle scraping
    search.py             throttled DuckDuckGo queries
    expansion.py          outbound-link harvesting from cached homepages
seeds/indian_brands.txt   hand-compiled seed list
output/                   result CSV + JSON + report.json (checked in)
```
