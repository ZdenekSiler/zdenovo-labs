"""The six stages of the overnight job. Each stage knows how to:

  plan(ws)            -> the tasks it needs (deterministic, so re-planning after a crash is a no-op)
  run(task, ws)       -> do one task and publish its artifact atomically
  verify(task, r, ws) -> re-read the artifact from disk and check it (a different code path
                         from run, so a bug in run doesn't grade itself)
  gate(ws)            -> whole-stage reconciliation; the next stage doesn't start until it passes

Sources (all fetched through PoliteFetcher, robots.txt enforced):
  bezrealitky.cz   flat listings for sale and rent, via the sitemap the site publishes
  ČÚZK RÚIAN       every address point in Czechia, CC-BY 4.0 (~3M rows, 6,258 municipal CSVs)
  ČSÚ              population by municipality (2000-2024) and completed dwellings (1997-2024)
"""
import csv
import io
import json
import random
import re
import statistics
import zipfile
from collections import Counter, defaultdict

from . import checks, geo
from .errors import Fatal, Retryable
from .queue import Task
from .workspace import Workspace, atomic_write, read_jsonl_file

SITEMAP_INDEX = "https://www.bezrealitky.cz/sitemap/sitemap.xml"
LISTING_RE = re.compile(r"https://www\.bezrealitky\.cz/nemovitosti-byty-domy/(\d+)-nabidka-(prodej|pronajem)-bytu-[^<\s]+")
RUIAN_URL = "https://vdp.cuzk.gov.cz/vymenny_format/csv/{date}_OB_ADR_csv.zip"
RUIAN_HEADER = "Kód ADM;Kód obce;Název obce;"
CSU = {
    "population": "https://csu.gov.cz/docs/107508/caaa6ad1-ddd1-c0df-1683-3c23ccf1f0c7/130149-25data052025.zip?version=1.0",
    "dwellings": "https://csu.gov.cz/docs/107508/f1c51fa3-594b-4cc2-c61d-3511620fed26/200068-25data090825.zip?version=1.0",
}
CSU_HEADERS = {
    "population": ["idhod", "hodnota", "stapro_kod", "pohlavi_cis", "pohlavi_kod", "vek_cis", "vek_kod",
                   "uzemi_cis", "uzemi_kod", "obdobi", "pohlavi_txt", "vek_txt", "uzemi_txt", "uzemi_typ"],
    "dwellings": ["idhod", "hodnota", "stapro_kod", "tb_cis", "tb_kod", "vuzemi_cis", "vuzemi_kod", "rok",
                  "casref_od", "casref_do", "stapro_txt", "tb_txt", "vuzemi_txt"],
}
PRAGUE = "554782"
GROWTH_FROM, GROWTH_TO = "2014", "2024"


class Stage:
    name = ""
    lease_s = 60.0
    max_attempts = 4
    uses_llm = False
    uses_network = False
    blocking = True   # a failed gate halts the night; False = enrichment, report without it

    def plan(self, ws: Workspace) -> list[tuple[str, dict]]:
        raise NotImplementedError

    def run(self, task: Task, ws: Workspace) -> dict:
        raise NotImplementedError

    def verify(self, task: Task, result: dict, ws: Workspace) -> list[str]:
        return []

    def gate(self, ws: Workspace) -> list[str]:
        dead = ws.queue.dead(self.name)
        return [f"{len(dead)} dead task(s): {[d['id'] for d in dead][:5]}"] if dead else []


def _csv_header(path, encoding="utf-8") -> list[str]:
    with zipfile.ZipFile(path) as z:
        first = sorted(z.namelist())[0]
        return next(csv.reader(io.TextIOWrapper(z.open(first), encoding=encoding)))


# ── 1. download: three government files and one sitemap ──────────────────────────
class Download(Stage):
    name = "download"
    lease_s = 900.0     # ČSÚ serves 20 MB in ~50 s; ČÚZK took 207 s for 63 MB and then dropped
    max_attempts = 6
    uses_network = True

    def plan(self, ws):
        return [(f"download:{what}", {"what": what}) for what in ("ruian", "population", "dwellings", "sitemap")]

    def run(self, task, ws):
        what = task.payload["what"]
        if what == "sitemap":
            return self._sitemap(ws)
        path = ws.raw / f"{what}.zip"
        if not path.exists():  # only complete files are renamed into place, so this means done
            ws.chaos.net_drop(task)
            url = RUIAN_URL.format(date=ws.cfg.ruian_date) if what == "ruian" else CSU[what]
            ws.fetcher.download(url, path, open_data=True)
            for note in ws.fetcher.notes:
                ws.queue.event("warning", note, task.id, task.worker)
            ws.fetcher.notes.clear()
        return {"bytes": path.stat().st_size}

    def _sitemap(self, ws):
        index = ws.fetcher.get(SITEMAP_INDEX)
        if index is None:
            raise Fatal("sitemap index is gone (404): the site changed, a human should look")
        found: dict[str, dict[str, str]] = {"prodej": {}, "pronajem": {}}
        for loc in re.findall(r"<loc>([^<]+sitemap_detail_\d+\.xml)</loc>", index.decode()):
            body = ws.fetcher.get(loc) or b""
            for m in LISTING_RE.finditer(body.decode()):
                found[m.group(2)][m.group(1)] = m.group(0)
        out = {offer: sorted(urls.items(), key=lambda kv: -int(kv[0])) for offer, urls in found.items()}
        atomic_write(ws.raw / "listing_urls.json", json.dumps(out))
        return {offer: len(v) for offer, v in out.items()}

    def verify(self, task, result, ws):
        what = task.payload["what"]
        if what == "sitemap":
            data = json.loads((ws.raw / "listing_urls.json").read_text())
            return [f"no {o} listings in sitemap" for o in ws.cfg.offers.split(",") if not data.get(o)]
        path = ws.raw / f"{what}.zip"
        if not zipfile.is_zipfile(path):
            path.unlink()
            return [f"{path.name} is not a valid zip; deleted for a clean re-download"]
        if what == "ruian":
            header = ";".join(_csv_header(path, "cp1250"))
            if not header.startswith(RUIAN_HEADER):
                raise Fatal(f"RÚIAN header changed: {header[:80]!r}")
        elif _csv_header(path) != CSU_HEADERS[what]:
            raise Fatal(f"ČSÚ {what} header changed: {_csv_header(path)}")
        return []


# ── 2. listings: one polite request per second ────────────────────────────────────
class Listings(Stage):
    """Only detail pages listed in the sitemap, never the search pages robots.txt disallows.
    Keeps a whitelist of fields. Never names, phones, emails or photos."""
    name = "listings"
    lease_s = 300.0
    uses_network = True

    def plan(self, ws):
        data = json.loads((ws.raw / "listing_urls.json").read_text())
        tasks = []
        for offer in ws.cfg.offers.split(","):
            urls = [url for _, url in data[offer]]
            if ws.cfg.max_per_offer:
                urls = urls[:ws.cfg.max_per_offer]  # newest first
            size = ws.cfg.listing_batch
            tasks += [(f"listings:{offer}:{i // size:04d}", {"offer": offer, "urls": urls[i:i + size]})
                      for i in range(0, len(urls), size)]
        return tasks

    @staticmethod
    def parse(body: bytes, advert_id: str) -> dict | None:
        m = re.search(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
        if not m:
            return None
        cache = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("apolloCache", {})
        return cache.get(f"Advert:{advert_id}")  # the page also caches *related* adverts

    def run(self, task, ws):
        offer = task.payload["offer"]
        ws.chaos.net_drop(task)
        cache = ws.listings / ".cache"
        cache.mkdir(exist_ok=True)
        stats, kept = Counter(), []
        for url in task.payload["urls"]:
            advert_id = re.search(r"/(\d+)-", url).group(1)
            # A retry must not hit the source again for pages it already has: the poison-pill
            # batch cost bezrealitky 100 requests before this cache existed. Entries are the
            # already-whitelisted, redacted record (or the skip reason), never the raw page.
            cached = cache / f"{advert_id}.json"
            if cached.exists():
                outcome = json.loads(cached.read_text())
            else:
                outcome = self._process(offer, advert_id, ws.fetcher.get(url))
                atomic_write(cached, json.dumps(outcome, ensure_ascii=False))
            if "skip" in outcome:
                stats[outcome["skip"]] += 1
            else:
                kept.append(outcome)
                stats["kept"] += 1
                stats["with_ruian_id" if outcome["ruian_id"] else "gps_only"] += 1
        sale = [r for r in kept if r["description"]]
        if sale and ws.chaos.fires(task, "poison"):    # regression test for the U+2028 fix
            sale[0] = {**sale[0], "description": sale[0]["description"] + "\u2028(chaos)"}
            kept = [sale[0] if r["id"] == sale[0]["id"] else r for r in kept]
        if sale and ws.chaos.fires(task, "pii_leak"):  # the redaction grader must catch this
            leaked = {**sale[0], "description": sale[0]["description"] + " Volejte 777 123 456."}
            kept = [leaked if r["id"] == leaked["id"] else r for r in kept]
        atomic_write(ws.listings / f"{task.id.replace(':', '-')}.jsonl",
                     "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
        return {"fetched": len(task.payload["urls"]), **stats}

    def _process(self, offer: str, advert_id: str, body: bytes | None) -> dict:
        if body is None:
            return {"skip": "gone"}
        a = self.parse(body, advert_id)
        if a is None:
            return {"skip": "unparsed"}
        if not a.get("active"):
            return {"skip": "inactive"}
        if not a.get("gps"):
            return {"skip": "no_gps"}
        if not geo.in_czechia(a["gps"]["lat"], a["gps"]["lng"]):
            return {"skip": "abroad"}  # bezrealitky also lists Germany: ~970 flats in the sitemap
        if not checks.plausible(offer, a.get("price"), a.get("surface")):
            return {"skip": "implausible"}
        return {
            "id": advert_id, "offer": offer, "price": a["price"], "surface": a["surface"],
            "disposition": a.get("disposition"), "condition": a.get("condition"),
            "penb": a.get("penb"),
            # about half the sellers give no house number, so no RÚIAN code: GPS joins those
            "ruian_id": str(a["ruianId"]) if a.get("ruianId") else None,
            "gps": [a["gps"]["lat"], a["gps"]["lng"]],
            "is_discounted": bool(a.get("isDiscounted")), "original_price": a.get("originalPrice"),
            "date_posted": a.get("datePosted"),
            # rent ads aren't analysed for seller signals, so their text isn't kept at all
            "description": checks.redact(a.get("description") or "")[:1500] if offer == "prodej" else "",
        }

    def verify(self, task, result, ws):
        rows = read_jsonl_file(ws.listings / f"{task.id.replace(':', '-')}.jsonl")
        kept = result.get("kept", 0)
        problems = [] if len(rows) == kept else [f"{len(rows)} rows on disk, {kept} reported"]
        for r in rows:
            if checks.find_contacts(r["description"]):
                problems.append(f"listing {r['id']}: contact data survived redaction")
            if not checks.plausible(r["offer"], r["price"], r["surface"]):
                problems.append(f"listing {r['id']}: implausible price/m²")
        return problems

    def gate(self, ws):
        problems = super().gate(ws)
        totals = Counter()
        for _, r in ws.queue.results(self.name):
            totals.update(r)
        if totals["fetched"] and totals["unparsed"] / totals["fetched"] > 0.05:
            problems.append(f"{totals['unparsed']} of {totals['fetched']} pages unparseable: did the page layout change?")
        if not totals["kept"]:
            problems.append("no listings kept")
        return problems


# ── 3. RÚIAN: three million address points, sharded across processes ──────────────
class RuianShards(Stage):
    """Per shard: address counts per municipality, the exact position of every address a
    listing points at, and a 500 m grid of which municipality owns which cell (for the
    listings that only have GPS)."""
    name = "ruian"
    lease_s = 30.0  # a shard takes ~2 s; the lease is how long a crashed shard waits to be redone

    def plan(self, ws):
        with zipfile.ZipFile(ws.raw / "ruian.zip") as z:
            names = sorted(n for n in z.namelist() if n.endswith(".csv"))
        size = ws.cfg.shard_files
        return [(f"ruian:{i // size:04d}", {"files": names[i:i + size]}) for i in range(0, len(names), size)]

    def run(self, task, ws):
        needed = {r["ruian_id"] for r in ws.read_jsonl(ws.listings) if r["ruian_id"]}
        addresses, names, adm, rows = Counter(), {}, {}, 0
        grid: dict[str, Counter] = defaultdict(Counter)
        with zipfile.ZipFile(ws.raw / "ruian.zip") as z:
            for name in task.payload["files"]:
                reader = io.TextIOWrapper(z.open(name), encoding="cp1250")
                next(reader)  # header
                for line in reader:
                    parts = line.split(";")
                    code, obec, names[obec] = parts[0], parts[1], parts[2]
                    rows += 1
                    addresses[obec] += 1
                    if parts[16] and parts[17]:
                        y, x = float(parts[16]), float(parts[17])
                        grid[geo.cell(y, x)][obec] += 1
                        if code in needed:
                            adm[code] = [obec, y, x]
        ws.chaos.crash(task)                    # die before publishing: lease expiry recovers it
        ws.chaos.hang(task, self.lease_s + 5)   # outlive the lease: fencing discards us
        if ws.chaos.fires(task, "corrupt"):     # the bug that doesn't throw: lose a municipality
            addresses.pop(next(iter(addresses)))
        atomic_write(ws.ruian / f"shard-{task.id.split(':')[1]}.json",
                     json.dumps({"rows": rows, "addresses": addresses, "names": names, "adm": adm, "grid": grid}))
        return {"rows": rows, "matched": len(adm)}

    def verify(self, task, result, ws):
        data = json.loads((ws.ruian / f"shard-{task.id.split(':')[1]}.json").read_text())
        with zipfile.ZipFile(ws.raw / "ruian.zip") as z:  # independent recount: lines, no parsing
            recount = sum(sum(1 for _ in z.open(n)) - 1 for n in task.payload["files"])
        counted = sum(data["addresses"].values())
        return [] if counted == recount == data["rows"] else [
            f"reconciliation: {counted:,} addresses aggregated, {recount:,} lines in the files"]

    def gate(self, ws):
        problems = super().gate(ws)
        covered = sum(len(p["files"]) for p, _ in ws.queue.results(self.name))
        with zipfile.ZipFile(ws.raw / "ruian.zip") as z:
            total = sum(1 for n in z.namelist() if n.endswith(".csv"))
        if covered != total:
            problems.append(f"shards cover {covered} of {total} municipal files")
        return problems


# ── 4. census: ČSÚ population and completed dwellings ─────────────────────────────
class Census(Stage):
    name = "census"
    lease_s = 300.0

    def plan(self, ws):
        return [("census:population", {"what": "population"}), ("census:dwellings", {"what": "dwellings"})]

    def run(self, task, ws):
        what = task.payload["what"]
        by_obec: dict[str, dict[str, int]] = defaultdict(dict)
        seen, duplicates, rows = set(), 0, 0
        with zipfile.ZipFile(ws.raw / f"{what}.zip") as z:
            reader = csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8"))
            for r in reader:
                rows += 1
                if what == "population":
                    if r["uzemi_cis"] != "43" or r["pohlavi_kod"] or r["vek_kod"]:
                        continue  # keep municipality totals only (no sex/age breakdown)
                    obec, year = r["uzemi_kod"], r["obdobi"][:4]
                else:
                    if r["vuzemi_cis"] != "43" or r["tb_kod"]:
                        continue  # municipality totals only (not houses vs apartment blocks)
                    obec, year = r["vuzemi_kod"], r["rok"]
                # The official dwellings file repeats 392 records under the same idhod (Prague
                # appears as both "Hlavní město Praha" and "Praha"). Summing would double them.
                if r["idhod"] in seen:
                    duplicates += 1
                    continue
                seen.add(r["idhod"])
                by_obec[obec][year] = int(r["hodnota"])
        atomic_write(ws.census / f"{what}.json", json.dumps({"by_obec": by_obec, "duplicates_dropped": duplicates}))
        return {"rows": rows, "municipalities": len(by_obec), "duplicates_dropped": duplicates}

    def verify(self, task, result, ws):
        data = json.loads((ws.census / f"{task.payload['what']}.json").read_text())["by_obec"]
        problems = [] if len(data) > 6000 else [f"only {len(data)} municipalities"]
        years = {GROWTH_FROM, GROWTH_TO} if task.payload["what"] == "population" else {GROWTH_TO}
        missing = years - set(data.get(PRAGUE, {}))
        if missing:
            problems.append(f"Prague has no data for {sorted(missing)}")
        return problems


# ── 5. seller signals (the LLM stage) ─────────────────────────────────────────────
SYSTEM = """You read Czech real-estate sale ads. For EVERY input key, and only those keys, return
an object with two fields.

"motivation":
  "urgent"      the seller says they are in a hurry: spěchá, rychlý prodej, nutno prodat
  "negotiable"  the price is explicitly open: cena k jednání, cena dohodou, sleva možná
  "neutral"     anything else (most ads)

"defects": true ONLY if the ad admits something is wrong NOW: vlhkost, plíseň, praskliny,
  zatéká, okna/rozvody/elektroinstalace k výměně, byt k rekonstrukci / před rekonstrukcí.
  false if the flat was already renovated (po rekonstrukci, zrekonstruovaný), is new
  (novostavba), or the ad says nothing about problems. Being in a hurry is NOT a defect.

Example output: {"k0": {"motivation": "neutral", "defects": false}}"""


class Signals(Stage):
    """Only sale ads, and only those the regex anchors can't decide. Two canaries with known
    answers ride in every batch; get one wrong and the whole batch is discarded."""
    name = "signals"
    blocking = False  # seller signals are enrichment: yields and pressure don't need them
    lease_s = 600.0  # the first run's 180 s lease expired under a slow 8B call and got fenced
    uses_llm = True
    rounds = 3
    canaries = 2

    def plan(self, ws):
        ads = sorted((r["id"], r["description"][:600]) for r in ws.read_jsonl(ws.listings)
                     if r["offer"] == "prodej" and r["description"] and not checks.anchor_signals(r["description"]))
        size = ws.cfg.signal_batch
        tasks = []
        for n, i in enumerate(range(0, len(ads), size)):
            rng = random.Random(n)  # deterministic: a re-plan after a crash yields identical tasks
            canaries = rng.sample(sorted(checks.CANARIES), self.canaries)
            batch = ads[i:i + size] + [(None, c) for c in canaries]
            rng.shuffle(batch)
            tasks.append((f"signals:{n:04d}", {
                "items": {f"k{j}": text for j, (_, text) in enumerate(batch)},
                "ids": {f"k{j}": lid for j, (lid, _) in enumerate(batch) if lid},
            }))
        return tasks

    def run(self, task, ws):
        items, ids = task.payload["items"], task.payload["ids"]
        prompt = json.dumps(items, ensure_ascii=False, indent=0)
        problems: list[str] = []
        for round_no in range(1, self.rounds + 1):
            ws.chaos.llm_down(task)
            user = prompt
            if problems:  # the agent loop: show the model exactly what the grader rejected
                user += "\n\nYour previous answer was rejected:\n- " + "\n- ".join(problems) + "\nFix it."
            # Cascade: if the small model has failed this batch twice, a bigger one gets a turn.
            escalate = ws.cfg.escalate_model and task.attempts >= 3
            answer = ws.llm.chat_json(SYSTEM, user, temperature=0.0 if task.attempts == 1 else 0.5,
                                      model=ws.cfg.escalate_model if escalate else None)
            if isinstance(answer, dict) and ids and ws.chaos.fires(task, "llm_garbage", round_no):
                answer = {**answer, next(iter(ids)): {"motivation": "desperate", "defects": "maybe"}}
            problems = checks.check_signals(items, answer)
            if any(p.startswith("canary") for p in problems):
                # Never tell the model the canary's answer: it would fix that key and teach us
                # nothing. Discard the whole attempt and let the queue retry it later.
                raise Retryable("canary failed: " + "; ".join(p for p in problems if p.startswith("canary"))[:300])
            if not problems:
                out = {ids[k]: answer[k] for k in ids}
                atomic_write(ws.signals / f"{task.id.replace(':', '-')}.json", json.dumps(out))
                return {"labelled": len(out), "rounds": round_no,
                        "model": ws.cfg.escalate_model if escalate else ws.cfg.model}
            ws.queue.event("self_correct", "; ".join(problems)[:300], task.id, task.worker)
        raise Retryable(f"still invalid after {self.rounds} rounds: " + "; ".join(problems)[:300])

    def verify(self, task, result, ws):
        on_disk = json.loads((ws.signals / f"{task.id.replace(':', '-')}.json").read_text())
        ids = task.payload["ids"]
        real = {k: task.payload["items"][k] for k in ids}
        return checks.check_signals(real, {k: on_disk.get(ids[k]) for k in ids})

    def gate(self, ws):
        """Dead batches are allowed up to the error budget: their ads get no signal and are
        left out of the signal analysis, rather than blocking the whole night."""
        sale = sum(1 for r in ws.read_jsonl(ws.listings) if r["offer"] == "prodej")
        lost = sum(len(d["payload"]["ids"]) for d in ws.queue.dead(self.name))
        share = 100 * lost / max(1, sale)
        if share > ws.cfg.error_budget_pct:
            return [f"{lost} sale ads ({share:.2f}%) without signals, budget is {ws.cfg.error_budget_pct}%"]
        if lost:
            ws.queue.event("warning", f"{lost} sale ads ({share:.2f}%) left without signals (within budget)")
        return []


# ── 6. reduce: join everything, compute the insight, narrate it ──────────────────
NARRATOR = """You write a 3-5 sentence plain-English summary of Czech flat-market statistics for a
morning report. The data are asking prices from one listing portal at one point in time: do not
describe trends over time. Use ONLY numbers that appear in the facts JSON, written exactly as given.
Return JSON: {"summary": "..."}"""


def _median(xs):
    return statistics.median(xs) if xs else None


class Reduce(Stage):
    name = "reduce"
    lease_s = 300.0
    uses_llm = True

    def plan(self, ws):
        return [("reduce:all", {})]

    def facts(self, ws) -> dict:
        cfg = ws.cfg
        adm, addresses, names = {}, Counter(), {}
        grid: dict[str, Counter] = defaultdict(Counter)
        for f in ws.ruian.glob("shard-*.json"):
            d = json.loads(f.read_text())
            adm.update(d["adm"])
            addresses.update(d["addresses"])
            names.update(d["names"])
            for k, v in d["grid"].items():
                grid[k].update(v)

        def by_gps(lat, lon):
            y, x = geo.wgs84_to_sjtsk(lat, lon)
            key = geo.cell(y, x)
            votes = grid.get(key) or sum((grid.get(n, Counter()) for n in geo.neighbours(key)), Counter())
            return (votes.most_common(1)[0][0] if votes else None), y, x
        pop = json.loads((ws.census / "population.json").read_text())["by_obec"]
        dwell = json.loads((ws.census / "dwellings.json").read_text())["by_obec"]
        # A failed enrichment gate means the labels we have aren't trustworthy as a set:
        # leave them out entirely rather than report on a biased 85%.
        signals_ok = ws.queue.gates().get("signals", (False, []))[0]
        signals = {}
        if signals_ok:
            for f in ws.signals.glob("*.json"):
                signals.update(json.loads(f.read_text()))

        listings = list(ws.read_jsonl(ws.listings))
        per_obec = defaultdict(lambda: {"prodej": [], "pronajem": []})
        joined, method, errors, agree = [], Counter(), [], []
        for r in listings:
            gps_obec, y, x = by_gps(*r["gps"])
            exact = adm.get(r["ruian_id"]) if r["ruian_id"] else None
            if exact:
                # Holdout: these listings have both keys, so they grade the GPS path.
                errors.append(((y - exact[1]) ** 2 + (x - exact[2]) ** 2) ** 0.5)
                agree.append(gps_obec == exact[0])
                obec = exact[0]
                method["ruian_id"] += 1
            else:
                obec = gps_obec
                method["gps" if obec else "unjoined"] += 1
            if obec:
                r["obec"], r["ppm2"] = obec, r["price"] / r["surface"]
                per_obec[obec][r["offer"]].append(r)
                joined.append(r)

        cities = []
        for obec, offers in per_obec.items():
            sale, rent = offers["prodej"], offers["pronajem"]
            if len(sale) < cfg.min_listings or len(rent) < cfg.min_listings:
                continue
            sale_m2 = _median([r["ppm2"] for r in sale])
            rent_m2 = _median([r["ppm2"] for r in rent])
            p = pop.get(obec, {})
            p0, p1 = p.get(GROWTH_FROM), p.get(GROWTH_TO)
            built = sum(n for y, n in dwell.get(obec, {}).items() if GROWTH_FROM < y <= GROWTH_TO)
            growth = (p1 - p0) if p0 and p1 else None
            cities.append({
                "city": names.get(obec, obec),
                "sale_listings": len(sale),
                "rent_listings": len(rent),
                "median_sale_czk_m2": round(sale_m2),
                "median_rent_czk_m2_month": round(rent_m2),
                "gross_yield_pct": round(1200 * rent_m2 / sale_m2, 1),
                "population_change_pct_2014_2024": round(100 * growth / p0, 1) if growth is not None else None,
                "dwellings_completed_2015_2024": built,
                "new_residents_per_new_dwelling": round(growth / built, 2) if growth is not None and built else None,
                "listings_per_1000_addresses": round(1000 * (len(sale) + len(rent)) / addresses[obec], 1),
            })
        cities.sort(key=lambda c: -c["gross_yield_pct"])

        # Within-city comparisons: each sale ad vs its own city's median, so Prague vs Ostrava
        # doesn't masquerade as "motivated sellers are cheaper".
        city_median = {o: _median([r["ppm2"] for r in v["prodej"]]) for o, v in per_obec.items()
                       if len(v["prodej"]) >= cfg.min_listings}
        groups = defaultdict(list)
        for r in joined:
            if r["offer"] != "prodej" or r["obec"] not in city_median:
                continue
            ratio = r["ppm2"] / city_median[r["obec"]]
            sig = (signals.get(r["id"]) or checks.anchor_signals(r["description"])) if signals_ok else None
            if sig:
                groups[f"motivation_{sig['motivation']}"].append(ratio)
                groups["defects_disclosed" if sig["defects"] else "no_defects_disclosed"].append(ratio)
            if r["condition"]:
                groups[f"condition_{r['condition'].lower()}"].append(ratio)
        vs_city = {k: {"listings": len(v), "median_vs_city_pct": round(100 * (_median(v) - 1), 1)}
                   for k, v in sorted(groups.items()) if len(v) >= cfg.min_listings}

        sale_all = [r for r in joined if r["offer"] == "prodej"]
        cut = [1 - r["price"] / r["original_price"] for r in sale_all
               if r["is_discounted"] and r["original_price"] and r["original_price"] > r["price"]]
        kept = Counter(r["offer"] for r in listings)
        return {
            "listings_kept": {"sale": kept["prodej"], "rent": kept["pronajem"]},
            "joined_to_ruian_pct": round(100 * len(joined) / max(1, len(listings)), 1),
            "join_method": dict(method),
            "gps_check": {
                "pairs": len(errors),
                "median_error_m": round(_median(errors), 1) if errors else None,
                "same_municipality_pct": round(100 * sum(agree) / len(agree), 1) if agree else None,
            },
            "ruian_addresses_scanned": sum(addresses.values()),
            "cities": cities,
            "seller_signals": "included" if signals_ok else "omitted: the signals gate failed (see report)",
            "sale_price_vs_city_median": vs_city,
            "price_cuts": {
                "share_of_sale_listings_cut_pct": round(100 * len(cut) / max(1, len(sale_all)), 1),
                "median_cut_pct": round(100 * _median(cut), 1) if cut else None,
            },
        }

    def run(self, task, ws):
        facts = self.facts(ws)
        atomic_write(ws.out / "facts.json", json.dumps(facts, indent=2, ensure_ascii=False))
        problems: list[str] = []
        for round_no in range(1, 4):
            user = json.dumps(facts, ensure_ascii=False)
            if problems:
                user += f"\n\nRejected: these numbers are not in the facts: {problems}. Rewrite."
            text = str(ws.llm.chat_json(NARRATOR, user).get("summary", ""))
            problems = checks.unsupported_numbers(text, facts)
            if text and not problems:
                atomic_write(ws.out / "summary.md", text + "\n")
                return {"narrator": "llm", "rounds": round_no, "cities": len(facts["cities"])}
            ws.queue.event("self_correct", f"unsupported numbers {problems}", task.id, task.worker)
        # Degrade, don't die: a template can't hallucinate. The report says which one you got.
        top = facts["cities"][0] if facts["cities"] else None
        text = (f"{facts['listings_kept']['sale']:,} sale and {facts['listings_kept']['rent']:,} rent listings. "
                + (f"Highest gross yield: {top['city']} ({top['gross_yield_pct']}%)." if top else "No city had enough listings."))
        atomic_write(ws.out / "summary.md", text + "\n")
        return {"narrator": "template", "rounds": 3, "cities": len(facts["cities"])}

    def verify(self, task, result, ws):
        facts = json.loads((ws.out / "facts.json").read_text())
        problems = checks.unsupported_numbers((ws.out / "summary.md").read_text(), facts)
        if facts["joined_to_ruian_pct"] < 95:
            problems.append(f"only {facts['joined_to_ruian_pct']}% of listings joined to RÚIAN (budget: 95%)")
        problems += checks.check_gps_join(facts["gps_check"])
        if not facts["cities"]:  # deterministic: retrying can't conjure listings
            raise Fatal(f"no city has {ws.cfg.min_listings}+ sale and rent listings; fetch more (--max-per-offer)")
        return problems


PIPELINE: list[Stage] = [Download(), Listings(), RuianShards(), Census(), Signals(), Reduce()]
STAGES = {s.name: s for s in PIPELINE}
