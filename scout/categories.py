"""Category classification from the product catalog and page text.

The assignment asks for *what the store sells* ("skincare", "men's
apparel"), so the classifier prefers a specific label over a generic one:

1. Score product_type / tags / product titles from /products.json against a
   keyword taxonomy.
2. Fall back to collection/nav labels found in the homepage HTML.
3. Fall back to homepage text keywords.

Returns (category, method) so the export can report provenance.
"""

from __future__ import annotations

import re
from collections import Counter

# keyword patterns -> canonical label; first match wins per keyword, but the
# overall winner is the label with the highest weighted hit count.
TAXONOMY: list[tuple[str, list[str]]] = [
    ("Sarees & Ethnic Wear", [r"saree", r"sari", r"lehenga", r"kurta", r"kurti",
                              r"ethnic", r"anarkali", r"salwar", r"sherwani",
                              r"dhoti", r"achkan", r"ghagra", r"dupatta"]),
    ("Women's Apparel", [r"\bwomen'?s? (?:clothing|apparel|wear|fashion|top|dress)\b",
                         r"\bladies\b", r"\bco-?ords?\b", r"\bdresses?\b", r"\bjumpsuits?\b",
                         r"\btops?\b", r"\bskirts?\b", r"\bcrop top\b"]),
    ("Men's Apparel", [r"\bmen'?s? (?:clothing|apparel|wear|fashion|t-?shirt|shirt)\b",
                       r"\bt-?shirts?\b", r"\bshirts?\b", r"\bjeans?\b", r"\btrousers?\b",
                       r"\bhoodies?\b", r"\bjoggers?\b", r"\bshorts\b", r"\bpolo\b"]),
    ("Kids & Baby", [r"\bkids?\b", r"\bbaby\b", r"\bchildren\b", r"\btoddler\b",
                     r"\bnewborn\b", r"\binfant\b", r"\bromper\b", r"\bdiaper\b"]),
    ("Skincare", [r"\bskincare\b", r"\bserum\b", r"\bmoisturi[sz]er\b", r"\bface ?wash\b",
                  r"\bsunscreen\b", r"\btoner\b", r"\bface ?oil\b", r"\bface ?mask\b",
                  r"\bcleanser\b", r"\bface ?cream\b", r"\bscrub\b", r"\bunder ?eye\b"]),
    ("Hair Care", [r"\bhair\b", r"\bshampoo\b", r"\bconditioner\b", r"\bhair ?oil\b",
                   r"\bhair ?mask\b", r"\bserum for hair\b"]),
    ("Beauty & Cosmetics", [r"\blipstick\b", r"\blip ?gloss\b", r"\bmakeup\b",
                            r"\bfoundation\b", r"\bmascara\b", r"\beyeliner\b",
                            r"\bhighlighter\b", r"\bblush\b", r"\bkajal\b",
                            r"\bnail ?polish\b", r"\beyeshadow\b", r"\bcompact\b",
                            r"\bbrush(es)?\b", r"\bprimer\b"]),
    ("Bath & Body", [r"\bsoap\b", r"\bbody ?wash\b", r"\bbody ?lotion\b", r"\bshower ?gel\b",
                     r"\bbath ?salt\b", r"\bbath ?bomb\b", r"\bbody ?butter\b", r"\bubtan\b"]),
    ("Fragrance", [r"\bperfume\b", r"\bfragrance\b", r"\battar\b", r"\bdeodorant\b",
                   r"\beau de\b", r"\bcologne\b", r"\bbody ?mist\b"]),
    ("Ayurveda & Wellness", [r"\bayurved", r"\bherbal\b", r"\bchurna\b", r"\bkwath\b",
                             r"\bcapsule\b", r"\bsupplement\b", r"\bvitamin\b",
                             r"\bprotein\b", r"\bimmunity\b", r"\bwellness\b",
                             r"\bpanchakarma\b", r"\bdetox\b"]),
    ("Food & Beverage", [r"\btea\b", r"\bcoffee\b", r"\bchai\b", r"\bspice", r"\bpickle",
                         r"\bmakhana\b", r"\bgranola\b", r"\bhoney\b", r"\bghee\b",
                         r"\bsnack\b", r"\bchocolate\b", r"\bmuesli\b",
                         r"\borganic food\b", r"\bdry ?fruits?\b",
                         r"\bjaggery\b", r"\batta\b", r"\bsauce\b", r"\bjam\b"]),
    ("Jewellery", [r"\bjewell?ery\b", r"\bring\b", r"\bnecklace\b", r"\bearring\b",
                   r"\bbracelet\b", r"\bbangle\b", r"\bpendant\b", r"\bajour\b",
                   r"\bnose ?pin\b", r"\bmaang ?tikka\b", r"\bchain\b", r"\bchoker\b"]),
    ("Bags & Luggage", [r"\bbag\b", r"\bbackpack\b", r"\bwallet\b", r"\btote\b",
                        r"\bclutch\b", r"\bhandbag\b", r"\bsling\b", r"\bluggage\b",
                        r"\btrolley\b", r"\blaptop ?sleeve\b", r"\bbelt bag\b"]),
    ("Footwear", [r"\bshoe", r"\bsandal\b", r"\bslipper\b", r"\bheels?\b", r"\bsneaker\b",
                  r"\bloafer\b", r"\bboots?\b", r"\bjuttis?\b", r"\bkolhapuris?\b",
                  r"\bflats?\b", r"\bsocks?\b"]),
    ("Home & Living", [r"\bbedsheet\b", r"\bcushion\b", r"\bcandle\b", r"\bdecor\b",
                       r"\bvase\b", r"\bwall ?art\b", r"\btableware\b", r"\bdinnerware\b",
                       r"\bplanter\b", r"\bmug\b", r"\bcarpet\b", r"\brug\b",
                       r"\bcurtain\b", r"\bhome ?furnish", r"\blamp\b", r"\bmattress\b",
                       r"\bpillow\b", r"\bbolster\b", r"\bquilt\b", r"\bdoormat\b",
                       r"\bstorage\b", r"\borganiser\b"]),
    ("Electronics & Gadgets", [r"\bearbuds?\b", r"\bheadphones?\b", r"\bspeaker\b",
                               r"\bcharger\b", r"\bcable\b", r"\bsmartwatch\b",
                               r"\bwearable\b", r"\belectronics\b", r"\bgadget\b",
                               r"\bpower ?bank\b", r"\bneckband\b", r"\btrimmer\b"]),
    ("Pets", [r"\bpet\b", r"\bdog\b", r"\bcat\b", r"\bpuppy\b", r"\bkitten\b",
              r"\bpet food\b", r"\bchew\b", r"\bpet care\b"]),
    ("Arts, Crafts & Stationery", [r"\bnotebook\b", r"\bstationery\b", r"\bjournal\b",
                                   r"\bplanner\b", r"\bpen\b", r"\bpencil\b", r"\bart\b",
                                   r"\bcraft\b", r"\bsticker\b", r"\bdiary\b",
                                   r"\bcalligraphy\b", r"\bsketch\b"]),
    ("Toys & Games", [r"\btoy", r"\bpuzzle\b", r"\bboard ?game\b", r"\bdoll\b",
                      r"\bbuilding ?blocks?\b", r"\blearning ?kit\b"]),
    ("Fitness & Sports", [r"\bfitness\b", r"\byoga\b", r"\bgym\b", r"\bdumbbell\b",
                          r"\bsports\b", r"\bathleisure\b", r"\bactivewear\b",
                          r"\bmat\b", r"\bskipping ?rope\b"]),
    ("Grocery & Staples", [r"\brice\b", r"\bdal\b", r"\batta\b", r"\boil\b",
                           r"\bstaples?\b", r"\bflour\b", r"\bpulses?\b", r"\bmillets?\b"]),
]


# per-label weights resolve ambiguity between overlapping categories
# (e.g. a pet store selling pet toys should be Pets, not Toys & Games)
WEIGHTS = {
    "Pets": 1.5,
    "Kids & Baby": 1.2,
    "Ayurveda & Wellness": 1.1,
}


def _score_text(text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    t = text.lower()
    for label, patterns in TAXONOMY:
        s = 0.0
        for pat in patterns:
            hits = len(re.findall(pat, t))
            if hits:
                s += min(hits, 8)
        if s:
            scores[label] = s * WEIGHTS.get(label, 1.0)
    return scores


def _strip_scripts(html: str) -> str:
    """Remove script/style/noscript blocks so cookie banners, JS and CSS
    cannot masquerade as catalog keywords."""
    return re.sub(
        r"<(script|style|noscript)\b.*?</\1>", " ", html, flags=re.S | re.I)


def classify(products: list[dict], homepage_html: str = "") -> tuple[str | None, str | None]:
    """Return (category, method)."""
    # 1. product catalog evidence (strongest)
    product_types: list[str] = []
    tags: list[str] = []
    titles: list[str] = []
    for p in products[:100]:
        if p.get("product_type"):
            product_types.append(str(p["product_type"]))
        tags.extend(str(t) for t in (p.get("tags") or [])[:10])
        if p.get("title"):
            titles.append(str(p["title"]))

    scores: dict[str, float] = {}
    pt_counts = Counter(t.strip().lower() for t in product_types if t.strip())
    # full pooled scoring: a single dominant product_type must beat the whole
    # catalog evidence, so every signal is scored together (a pet store whose
    # most common type is "Toy" must still classify as Pets)
    if product_types:
        s = _score_text(" ".join(product_types))
        for k, v in s.items():
            scores[k] = scores.get(k, 0) + v * 2
    if tags:
        s = _score_text(" ".join(tags[:400]))
        for k, v in s.items():
            scores[k] = scores.get(k, 0) + v * 1.5
    if titles:
        s = _score_text(" ".join(titles[:80]))
        for k, v in s.items():
            scores[k] = scores.get(k, 0) + v

    # 2. homepage nav / collection labels (medium)
    if not scores and homepage_html:
        nav_texts = re.findall(
            r'<a[^>]+href=["\'][^"\']*/collections/[^"\']*["\'][^>]*>([^<]{3,40})<',
            homepage_html, re.I)
        if nav_texts:
            s = _score_text(" ".join(nav_texts[:60]))
            for k, v in s.items():
                scores[k] = scores.get(k, 0) + v

    # 3. homepage text (weakest)
    if not scores and homepage_html:
        text = re.sub(r"<[^>]+>", " ", _strip_scripts(homepage_html))
        s = _score_text(text[:20000])
        scores.update(s)

    if not scores:
        # unmapped but real product_type - use it verbatim if plausible
        if pt_counts:
            top = pt_counts.most_common(1)[0][0]
            if 2 <= len(top) <= 30 and re.match(r"^[a-zA-Z][a-zA-Z &'-]+$", top):
                return top.title(), "product_type_verbatim"
        return None, None

    best, _ = max(scores.items(), key=lambda kv: kv[1])
    method = "catalog" if (products and (product_types or tags or titles)) else "homepage"
    return best, method
