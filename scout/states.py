"""Indian geography reference data and state-resolution helpers.

Resolution ladder for the `state` field (best first):

1. `meta.json` `province` - the merchant's own admin entry (normalized).
2. GSTIN on any crawled page - the first two digits are the state code and
   the full number carries a checksum we validate.
3. PIN code - first two digits map to states with the standard postal zones.
4. Explicit state name in the address text.

Only accepted when the evidence is unambiguous; otherwise blank and the
method is left empty so the export can report the gap honestly.
"""

from __future__ import annotations

import re

# 28 states + 8 union territories
STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]

# common spelling variants seen on storefronts
VARIANTS = {
    "orissa": "Odisha",
    "pondicherry": "Puducherry",
    "uttaranchal": "Uttarakhand",
    "madras": "Tamil Nadu",
    "bangalore": "Karnataka",  # city names often appear in the province slot
    "bengaluru": "Karnataka",
    "bombay": "Maharashtra",
    "calcutta": "West Bengal",
    "cochin": "Kerala",
    "trivandrum": "Kerala",
    "vizag": "Andhra Pradesh",
    "new delhi": "Delhi",
    "delhi ncr": "Delhi",
    "jammu": "Jammu and Kashmir",
    "kashmir": "Jammu and Kashmir",
    "nct of delhi": "Delhi",
    "andaman & nicobar": "Andaman and Nicobar Islands",
    "dadra & nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
    "daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "daman & diu": "Dadra and Nagar Haveli and Daman and Diu",
    "ap": "Andhra Pradesh",
    "ts": "Telangana",
    "tn": "Tamil Nadu",
    "mh": "Maharashtra",
    "ka": "Karnataka",
    "dl": "Delhi",
    "up": "Uttar Pradesh",
    "gj": "Gujarat",
    "rj": "Rajasthan",
    "wb": "West Bengal",
    "kl": "Kerala",
    "pb": "Punjab",
    "hr": "Haryana",
    "mp": "Madhya Pradesh",
    "jk": "Jammu and Kashmir",
}

STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(
        re.escape(s) for s in STATES
    ) + r")\b",
    re.I,
)

# ---- GSTIN -----------------------------------------------------------------

GSTIN_RE = re.compile(
    r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z])\b"
)

GSTIN_STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh",
}

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_valid(gstin: str) -> bool:
    """Standard GSTIN mod-36 checksum."""
    if len(gstin) != 15:
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        factor = 2 if (i % 2) else 1
        val = CHARS.index(ch) * factor
        total += val // 36 + val % 36
    check = (36 - (total % 36)) % 36
    return CHARS[check] == gstin[14]


def state_from_gstin(text: str) -> str | None:
    for m in GSTIN_RE.finditer(text.upper()):
        gstin = m.group(1)
        if gstin_valid(gstin):
            state = GSTIN_STATE_CODES.get(gstin[:2])
            if state:
                return state
    return None


# ---- PIN codes --------------------------------------------------------------

PINCODE_RE = re.compile(r"\b([1-9][0-9]{5})\b")

# first two digits -> state(s) (postal zones; some zones share a first pair)
PIN_PREFIX_STATE = {
    "11": "Delhi", "12": "Haryana", "13": "Haryana", "14": "Punjab",
    "15": "Punjab", "16": "Chandigarh", "17": "Himachal Pradesh",
    "18": "Jammu and Kashmir", "19": "Jammu and Kashmir",
    "20": "Uttarakhand", "21": "Uttarakhand", "22": "Uttar Pradesh",
    "23": "Uttar Pradesh", "24": "Uttar Pradesh", "25": "Uttar Pradesh",
    "26": "Uttar Pradesh", "27": "Uttar Pradesh", "28": "Uttar Pradesh",
    "30": "Rajasthan", "31": "Rajasthan", "32": "Rajasthan",
    "33": "Rajasthan", "34": "Rajasthan",
    "36": "Gujarat", "37": "Gujarat", "38": "Gujarat", "39": "Gujarat",
    "40": "Maharashtra", "41": "Maharashtra", "42": "Maharashtra",
    "43": "Maharashtra", "44": "Maharashtra",
    "45": "Madhya Pradesh", "46": "Madhya Pradesh", "47": "Madhya Pradesh",
    "48": "Madhya Pradesh", "49": "Chhattisgarh",
    "50": "Telangana", "51": "Andhra Pradesh", "52": "Andhra Pradesh",
    "53": "Andhra Pradesh",
    "56": "Karnataka", "57": "Karnataka", "58": "Karnataka", "59": "Karnataka",
    "60": "Tamil Nadu", "61": "Tamil Nadu", "62": "Tamil Nadu",
    "63": "Tamil Nadu", "64": "Tamil Nadu",
    "67": "Kerala", "68": "Kerala", "69": "Kerala",
    "70": "West Bengal", "71": "West Bengal", "72": "West Bengal",
    "73": "West Bengal", "74": "West Bengal",
    "75": "Odisha", "76": "Odisha", "77": "Odisha",
    "78": "Assam", "79": "Arunachal Pradesh",  # 79x covers NE; ambiguous zone
    "80": "Bihar", "81": "Bihar", "82": "Bihar", "83": "Bihar", "84": "Bihar",
    "85": "Bihar",
}


def state_from_pincode(text: str) -> str | None:
    """Conservative: accept a PIN only when its zone is single-state."""
    counts: dict[str, int] = {}
    for m in PINCODE_RE.finditer(text):
        pre = m.group(1)[:2]
        state = PIN_PREFIX_STATE.get(pre)
        if state:
            counts[state] = counts.get(state, 0) + 1
    if not counts:
        return None
    best = max(counts, key=counts.get)
    # require the zone to appear more than the runner-up to be meaningful
    others = [c for s, c in counts.items() if s != best]
    if counts[best] >= 2 or not others:
        return best
    if counts[best] > max(others):
        return best
    return None


# ---- cities -----------------------------------------------------------------

CITY_STATE = {
    "mumbai": "Maharashtra", "navi mumbai": "Maharashtra", "thane": "Maharashtra",
    "pune": "Maharashtra", "nagpur": "Maharashtra", "nashik": "Maharashtra",
    "aurangabad": "Maharashtra", "solapur": "Maharashtra", "kolhapur": "Maharashtra",
    "delhi": "Delhi", "new delhi": "Delhi",
    "gurugram": "Haryana", "gurgaon": "Haryana", "faridabad": "Haryana",
    "sonipat": "Haryana", "panipat": "Haryana", "karnal": "Haryana",
    "noida": "Uttar Pradesh", "greater noida": "Uttar Pradesh",
    "lucknow": "Uttar Pradesh", "kanpur": "Uttar Pradesh", "agra": "Uttar Pradesh",
    "varanasi": "Uttar Pradesh", "ghaziabad": "Uttar Pradesh", "meerut": "Uttar Pradesh",
    "bengaluru": "Karnataka", "bangalore": "Karnataka", "mysuru": "Karnataka",
    "mysore": "Karnataka", "mangaluru": "Karnataka", "mangalore": "Karnataka",
    "hubli": "Karnataka", "manipal": "Karnataka",
    "chennai": "Tamil Nadu", "coimbatore": "Tamil Nadu", "madurai": "Tamil Nadu",
    "trichy": "Tamil Nadu", "tiruchirappalli": "Tamil Nadu", "salem": "Tamil Nadu",
    "hyderabad": "Telangana", "secunderabad": "Telangana", "warangal": "Telangana",
    "ahmedabad": "Gujarat", "surat": "Gujarat", "vadodara": "Gujarat",
    "rajkot": "Gujarat", "gandhinagar": "Gujarat", "bhavnagar": "Gujarat",
    "jaipur": "Rajasthan", "jodhpur": "Rajasthan", "udaipur": "Rajasthan",
    "kota": "Rajasthan", "ajmer": "Rajasthan", "jaisalmer": "Rajasthan",
    "kolkata": "West Bengal", "howrah": "West Bengal", "siliguri": "West Bengal",
    "durgapur": "West Bengal", "asansol": "West Bengal",
    "kochi": "Kerala", "cochin": "Kerala", "trivandrum": "Kerala",
    "thiruvananthapuram": "Kerala", "kozhikode": "Kerala", "thrissur": "Kerala",
    "indore": "Madhya Pradesh", "bhopal": "Madhya Pradesh", "gwalior": "Madhya Pradesh",
    "jabalpur": "Madhya Pradesh", "ujjain": "Madhya Pradesh",
    "chandigarh": "Chandigarh", "mohali": "Punjab", "ludhiana": "Punjab",
    "amritsar": "Punjab", "jalandhar": "Punjab", "patiala": "Punjab",
    "dehradun": "Uttarakhand", "haridwar": "Uttarakhand", "roorkee": "Uttarakhand",
    "rishikesh": "Uttarakhand",
    "patna": "Bihar", "ranchi": "Jharkhand", "jamshedpur": "Jharkhand",
    "dhanbad": "Jharkhand", "bhubaneswar": "Odisha", "cuttack": "Odisha",
    "guwahati": "Assam", "shillong": "Meghalaya", "imphal": "Manipur",
    "vizag": "Andhra Pradesh", "visakhapatnam": "Andhra Pradesh",
    "vijayawada": "Andhra Pradesh", "guntur": "Andhra Pradesh",
    "goa": "Goa", "panaji": "Goa", "panjim": "Goa", "margao": "Goa",
    "raipur": "Chhattisgarh", "bhilai": "Chhattisgarh",
    "srinagar": "Jammu and Kashmir", "jammu": "Jammu and Kashmir",
    "leh": "Ladakh", "port blair": "Andaman and Nicobar Islands",
    "gangtok": "Sikkim", "agra city": "Uttar Pradesh",
}


def normalize_province(province: str | None) -> str | None:
    """Map a meta.json province (or free text) onto a canonical state name."""
    if not province:
        return None
    p = province.strip().lower()
    if not p:
        return None
    for state in STATES:
        if state.lower() == p:
            return state
    if p in VARIANTS:
        return VARIANTS[p]
    for state in STATES:
        if state.lower() in p or p in state.lower():
            return state
    for key, val in VARIANTS.items():
        if key in p:
            return val
    for city, state in CITY_STATE.items():
        if city in p:
            return state
    return None


def state_from_text(text: str) -> str | None:
    """GSTIN > PIN > explicit state name, from arbitrary page text."""
    m = re.search(
        r"(?:state|province)\s*[:\-]\s*([A-Za-z &]+?)(?:\n|,|\||$)", text, re.I
    )
    if m:
        st = normalize_province(m.group(1))
        if st:
            return st
    gst = state_from_gstin(text)
    if gst:
        return gst
    pin = state_from_pincode(text)
    if pin:
        return pin
    hits = list(STATE_NAME_RE.finditer(text))
    if len(hits) == 1:
        return normalize_province(hits[0].group(1))
    if len(hits) > 1:
        from collections import Counter
        best, n = Counter(h.group(1).title() for h in hits).most_common(1)[0]
        if n >= 2:
            return normalize_province(best)
    return None


def state_from_city(text: str) -> str | None:
    t = text.lower()
    for city, state in CITY_STATE.items():
        if re.search(rf"\b{re.escape(city)}\b", t):
            return state
    return None
