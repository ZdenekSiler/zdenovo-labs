"""The graders. Deterministic, boring and deliberately kept in one file.

Agents may edit almost anything in this lab. They may not edit this file: `.claude/settings.json`
denies Edit on it (which covers Write too). An agent that can rewrite its own grader will,
eventually, at 3am, "fix" a failing gate by loosening the check. Nobody has to be malicious
for that to happen.

Compliance lives here too. Whether a URL may be fetched is a grader's decision, not a worker's.
"""
import re

# ── robots.txt (RFC 9309) ────────────────────────────────────────────────────────
# Not urllib.robotparser: the stdlib matcher ignores `*` wildcards, so for bezrealitky's
# "Disallow: /vyhledat*" it answers can_fetch("/vyhledat?x=1") == True. A crawler that trusts
# it walks straight through an opt-out.


def parse_robots(text: str) -> dict[str, list[tuple[str, str]]]:
    """user-agent token (lowercase) -> [(allow|disallow, path pattern)]"""
    groups: dict[str, list[tuple[str, str]]] = {}
    agents: list[str] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:  # a user-agent line after rules starts a new group
                agents, in_rules = [], False
            agents.append(value.lower())
            groups.setdefault(value.lower(), [])
        elif key in ("allow", "disallow"):
            in_rules = True
            for a in agents:
                groups[a].append((key, value))
    return groups


def _pattern_matches(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    regex = "".join(".*" if c == "*" else re.escape(c) for c in pattern.rstrip("$"))
    return re.match(regex + ("$" if anchored else ""), path) is not None


def robots_allows(groups: dict, user_agent: str, path: str) -> bool:
    """Longest matching rule wins; on a tie, allow wins. No matching rule = allowed."""
    token = user_agent.split("/")[0].lower()
    rules = next((r for a, r in groups.items() if a != "*" and a in token), groups.get("*", []))
    best: tuple[int, bool] = (-1, True)
    for kind, pattern in rules:
        if kind == "disallow" and pattern == "":
            continue  # "Disallow:" with no path allows everything
        if _pattern_matches(pattern, path):
            length, allow = len(pattern), kind == "allow"
            if length > best[0] or (length == best[0] and allow):
                best = (length, allow)
    return best[1]


# ── personal data ────────────────────────────────────────────────────────────────
# Bezrealitky's terms (3.13) allow using personal data only to deal over the property. The
# lab never keeps names or contact fields, and redacts phones and emails out of free text.
PHONE_RE = re.compile(r"(?:\+?420[\s-]?)?\b\d{3}[\s-]?\d{3}[\s-]?\d{3}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def redact(text: str) -> str:
    return EMAIL_RE.sub("[email]", PHONE_RE.sub("[phone]", text))


def find_contacts(text: str) -> list[str]:
    return PHONE_RE.findall(text) + EMAIL_RE.findall(text)


# ── plausibility bounds for asking prices (CZK per m²) ──────────────────────────
BOUNDS = {"prodej": (15_000, 600_000), "pronajem": (60, 2_000)}  # sale price / monthly rent


def plausible(offer: str, price: float, surface: float) -> bool:
    if not price or not surface or surface < 10:
        return False
    lo, hi = BOUNDS[offer]
    return lo <= price / surface <= hi


# ── seller signals: the LLM's job ────────────────────────────────────────────────
# The listing JSON already has condition, energy class and price cuts as structured fields,
# so the model isn't asked for any of them. It only reads what's buried in the free text:
# is the seller in a hurry, and does the ad admit to defects?
MOTIVATION = ("urgent", "negotiable", "neutral")

# Anything a regex can decide, a regex decides. These only fire on unambiguous phrasing.
URGENT_RE = re.compile(r"\bspěchá\b|nutno (rychle )?prodat|rychl\w* prodej|prodej z důvodu (stěhování|exekuce|dědictví)", re.I)
NEGOTIABLE_RE = re.compile(r"cena (je )?k (jednání|diskusi)|cena dohodou|(možná|možnost) slev", re.I)
DEFECT_RE = re.compile(r"vlhk|plís|zatéká|praskl|k (výměně|opravě)|nutn\w* (oprav|rekonstrukc)|před rekonstrukcí", re.I)


def anchor_signals(text: str) -> dict | None:
    """Return labels only when the regexes leave nothing to judge, else None (ask the model)."""
    urgent, negotiable = bool(URGENT_RE.search(text)), bool(NEGOTIABLE_RE.search(text))
    if urgent == negotiable or not DEFECT_RE.search(text):
        return None  # both or neither motivation phrase, or no defect evidence either way
    return {"motivation": "urgent" if urgent else "negotiable", "defects": True}


# Canaries: synthetic ads with known answers, mixed into every batch. Unambiguous on purpose.
# (v1 of this lab learned that the hard way: a canary the grader gets wrong is worse than none.)
CANARIES = {
    "Spěchá! Nutno prodat do konce měsíce kvůli stěhování do zahraničí. Byt 2+kk, cihla, 3. patro.":
        {"motivation": "urgent", "defects": False},
    "Cena k jednání, rozumnou nabídku zvážím. Byt 3+1 po kompletní rekonstrukci, nová kuchyň.":
        {"motivation": "negotiable", "defects": False},
    "Nabízím k prodeji světlý byt 2+1 s balkonem v klidné části města, nová okna, zateplený dům.":
        {"motivation": "neutral", "defects": False},
    "Byt 1+1 v původním stavu. Ve sklepě je vlhkost, okna a elektroinstalace jsou k výměně.":
        {"motivation": "neutral", "defects": True},
    "Rychlý prodej, cena výrazně snížena. Upozorňuji, že v koupelně je plíseň a strop praská.":
        {"motivation": "urgent", "defects": True},
    "Moderní byt 4+kk v novostavbě s terasou, garážové stání a sklep v ceně.":
        {"motivation": "neutral", "defects": False},
}


def check_signals(batch: dict[str, str], answer: object) -> list[str]:
    """batch: key -> ad text. answer: whatever the model returned."""
    if not isinstance(answer, dict):
        return [f"expected a JSON object, got {type(answer).__name__}"]
    problems = []
    missing = sorted(set(batch) - set(answer))
    extra = sorted(set(answer) - set(batch))
    if missing:
        problems.append(f"missing keys: {missing}")
    if extra:
        problems.append(f"unexpected keys (answer only the given keys): {extra}")
    for key, text in batch.items():
        if key not in answer:
            continue
        value = answer[key]
        if not isinstance(value, dict):
            problems.append(f"{key}: expected an object with motivation and defects, got {value!r}")
            continue
        motivation, defects = value.get("motivation"), value.get("defects")
        if motivation not in MOTIVATION:
            problems.append(f"{key}: motivation {motivation!r} is not one of {list(MOTIVATION)}")
        if not isinstance(defects, bool):
            problems.append(f"{key}: defects must be true or false, got {defects!r}")
        known = CANARIES.get(text)
        valid = motivation in MOTIVATION and isinstance(defects, bool)
        if known and valid and known != {"motivation": motivation, "defects": defects}:
            problems.append(f"canary {key}: got {motivation}/{defects}, "
                            f"known answer {known['motivation']}/{known['defects']}")
    return problems


# ── GPS join: graded against the listings that have both GPS and an exact address ──
GPS_MAX_MEDIAN_ERROR_M = 250
GPS_MIN_AGREEMENT_PCT = 95.0


def check_gps_join(gps_check: dict) -> list[str]:
    if not gps_check["pairs"]:
        return ["no listing has both GPS and a RÚIAN address: the GPS join can't be graded"]
    problems = []
    if gps_check["median_error_m"] > GPS_MAX_MEDIAN_ERROR_M:
        problems.append(f"GPS -> S-JTSK median error {gps_check['median_error_m']} m "
                        f"(max {GPS_MAX_MEDIAN_ERROR_M}): is the coordinate conversion broken?")
    if gps_check["same_municipality_pct"] < GPS_MIN_AGREEMENT_PCT:
        problems.append(f"GPS join agrees with the exact join for only {gps_check['same_municipality_pct']}% "
                        f"of listings (min {GPS_MIN_AGREEMENT_PCT}%)")
    return problems


# ── narrator: every number must be traceable ─────────────────────────────────────
NUMBER_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)(?:\s*(million|milion\w*|thousand|tisíc\w*|billion|miliard\w*))?", re.I)
SCALE = {"m": 1e6, "t": 1e3, "b": 1e9}  # by first letter: million/milion, thousand/tisíc, billion/miliarda


def unsupported_numbers(text: str, facts: dict) -> list[str]:
    """Every number the narrator writes must exist in the facts it was given. Years and small
    ordinals ("top 3") are allowed, everything else is a claim and must be traceable."""
    allowed = set()
    for value in _walk(facts):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            allowed.add(_canon(value))
            if isinstance(value, float):
                allowed.add(_canon(round(value)))
                allowed.add(_canon(round(value, 1)))
    bad = []
    for m in NUMBER_RE.finditer(text):
        # "2.3 million" is a claim about 2,300,000, not a small number. The first version let
        # it through, and the narrator promptly wrote "over 2.3 million addresses" for 3,022,397.
        n = float(m.group(1).replace(",", "")) * (SCALE[m.group(2)[0].lower()] if m.group(2) else 1)
        if n <= 10 or (1990 <= n <= 2100 and n.is_integer()):
            continue
        if _canon(n) not in allowed:
            bad.append(m.group(0))
    return bad


def _canon(x: float) -> str:
    return f"{float(x):.1f}"


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj
