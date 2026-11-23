import json
ET = "America/New_York (10:00 AM ET expiration)"
media = [
    {"name": "AP race calls", "url": "https://apnews.com/hub/election-results", "latency": "minutes after poll close", "cost": "free"},
    {"name": "Decision Desk HQ", "url": "https://decisiondeskhq.com/", "latency": "minutes after poll close", "cost": "free web"},
    {"name": "NYT/CNN/Fox/NBC/CBS/ABC election pages", "url": "https://www.nytimes.com/section/politics", "latency": "minutes", "cost": "free"},
]
acc = ("Accelerated resolution when >=4 of 8 Designated Media Sources (NYT, AP, DDHQ, CNN, Fox, NBC, CBS, ABC) "
       "call it and none contradict; sources replaceable at Exchange sole discretion; Kalshi may open Market Outcome "
       "Review or change Source Agency/Underlying after first trading day (Rule 7.2).")
cards = {}
cards["SENATETX"] = dict(
    series="SENATETX",
    settlement_source="United States Congress (congress.gov): person sworn in for Texas Senate seat; accelerated path via media calls",
    settlement_variable="Party of first candidate who presents certificate of election and is sworn in for the Texas Senate seat for term beginning 2027 (replacement's party if vacancy before seating; election-day party if party switch)",
    timezone=ET,
    tie_handling="unknown (recount/contested outcomes not specified; falls to Kalshi review)",
    rounding="n/a (binary)",
    trading_close_vs_settlement="Last trading time = expiration time (10:00 AM ET); can close early on accelerated determination (can_close_early=1); settlement within 1 day after expiration (timer 1800s). Listed close_time 2027-11-03 is a backstop (a year after popular vote); real resolution expected shortly after the Nov 3 2026 election.",
    early_close_or_void_clauses=[acc, "Kalshi sole-discretion Market Outcome Review; no explicit void or no-data clause"],
    title_vs_rules_divergences=[
        "Title 'win the Senate race' vs rule 'sworn in' (Jan 2027); accelerated media-call path overrides so effective settlement is media consensus, not swearing-in",
        "close_time 2027-11-03 is a year after the election; actual close earlier on acceleration"],
    public_feeds_for_settlement_variable=media + [{"name": "Texas SOS election night results", "url": "https://results.texas-election.com/", "latency": "real-time county uploads", "cost": "free"}],
    observability_before_settlement="high",
    toxicity_flags=[
        "election-night counts are public; media desks/bots race on calls; informed flow from tally/decision-desk staff (prohibited, unenforceable)",
        "acceleration trigger (4 of 8 media) causes abrupt jump to settlement; resting quotes picked off",
        "Exchange can swap sources/underlying at its discretion",
        "long-dated illiquid pre-election tails"],
    notes="Rules from SENATEPARTY.pdf. Edge decays within seconds of AP/DDHQ call. Position accountability $250k/strike.")
cards["CONTROLH"] = dict(
    series="CONTROLH",
    settlement_source="congress.gov (party of Speaker of the House) on Feb 1 2027 10:00 AM ET; accelerated via media consensus calls",
    settlement_variable="Party of the Speaker of the House on expiration date (Feb 1, 2027, 10:00 AM ET) per congress.gov; early path: >=4 of 8 media sources project House control",
    timezone=ET,
    tie_handling="Independents: Exchange considers caucus decisions; vacancy/tie specifics unknown; Speaker-based",
    rounding="n/a (binary)",
    trading_close_vs_settlement="Last trading = expiration (Feb 1 2027 10:00 ET) unless accelerated; settlement no later than next day (timer 3600s). Post-expiration revisions ignored.",
    early_close_or_void_clauses=[acc, "Contingencies: Rule 6.3(c) review; Rule 7.2 source/underlying replacement"],
    title_vs_rules_divergences=[
        "Title 'win the House in 2026' vs rule: Speaker's party on Feb 1 2027 (control = Speaker, not seat majority); Speaker could differ from plurality party",
        "rules_secondary: early determination by media-call projection of control (seat majority) while fallback is Speaker identity: two definitions",
        "Order Size term (multiples of 5,000 contracts) looks like template carryover"],
    public_feeds_for_settlement_variable=media + [
        {"name": "congress.gov members", "url": "https://www.congress.gov/members", "latency": "hours-days", "cost": "free"},
        {"name": "Clerk of the House", "url": "https://clerk.house.gov/", "latency": "official, after swearing-in", "cost": "free"}],
    observability_before_settlement="high",
    toxicity_flags=[
        "race counts public; media calls move price instantly on election night",
        "Speaker-vs-majority definition gap invites late disputes/review",
        "acceleration needs 4 of 8 media; a contradiction delays it",
        "Exchange may replace sources/underlying at sole discretion",
        "high volume (~$3M/day) with sophisticated model participants"],
    notes="Rules from CONTROL.pdf. Pre-election price is a forecast with no hidden variable; post-election fully observable.")
cards["KXBALANCEPOWERCOMBO"] = dict(
    series="KXBALANCEPOWERCOMBO",
    settlement_source="Component rulesets (CONTROL for House and Senate -> congress.gov / media calls) under POLITICALECONOMICEVENTS umbrella; listed BLS/Fed sources are template boilerplate",
    settlement_variable="Conjunction (AND) of House control party and Senate control party (Speaker / President Pro Tempore party) for the 2027 term, each per CONTROL rules",
    timezone=ET,
    tie_handling="Each component per CONTROL (Independents via caucus); any No or impossible component => combo No",
    rounding="n/a (binary)",
    trading_close_vs_settlement="close 2027-02-01 15:00Z, expiration 2027-02-08 15:00Z, timer 1800s; umbrella terms say last trading one minute before expected final data release; components may accelerate so combo can settle early.",
    early_close_or_void_clauses=[
        "Each CONTROL component may resolve accelerated (4 of 8 media sources)",
        "Any component No/impossible => immediate No (early No)",
        "Kalshi review (Rule 7.1) and discretion to set payout if value cannot be determined",
        "Rule 7.2 source replacement"],
    title_vs_rules_divergences=[
        "settlement_sources list BLS jobs report, CPI, FOMC calendar: irrelevant to balance of power (template carryover)",
        "Title 'for Feb 2027' control but underlying is party of Speaker/PPT per CONTROL, not seat majority",
        "Expiration Feb 8 vs CONTROL expiration Feb 1"],
    public_feeds_for_settlement_variable=media + [{"name": "congress.gov", "url": "https://www.congress.gov/", "latency": "hours-days", "cost": "free"}],
    observability_before_settlement="high",
    toxicity_flags=[
        "combo of two correlated public-information outcomes; must stay coherent with CONTROLH/CONTROLS legs (leg arbitrage)",
        "component media calls resolve abruptly; early-No on any leg",
        "template/unclear sources raise review/discretion risk",
        "thin wide tails (yes ~0.006)"],
    notes="Arb-consistency check against CONTROLH and CONTROLS recommended. Umbrella terms POLITICALECONOMICEVENTS.pdf.")
cards["KXMLB"] = dict(
    series="KXMLB",
    settlement_source="Governing league (MLB) plus AP, ESPN, WSJ, Fox Sports (TITLE.pdf Source Agencies); series record lists ESPN, Fox Sports, AP",
    settlement_variable="Team reported as holder of the title (World Series champion) per the first official final result of the event",
    timezone=ET + "; sample close_time 2028-10-31 04:00Z",
    tie_handling="Multiple title holders: $1/N rounded down to nearest cent, No gets remainder; cancelled/no-contest: split among eligible teams",
    rounding="Yes payout = $1/N rounded down to nearest cent in split cases",
    trading_close_vs_settlement="Last trading = expiration; latest expiration is day after official final result; sample markets show close/expiration 2028-10-31 04:00Z as backstop, market closes earlier on resolution (can_close_early=1); timer 300s.",
    early_close_or_void_clauses=[
        "Postponed/suspended: stays open until winner reported or 2 years after scheduled date",
        "Cancelled/no contest: pro-rata split among non-eliminated teams with strikes",
        "Forfeit or disqualification before expiry => No; post-expiry revisions ignored",
        "Team eliminated per Source Agency => resolves No (early No)",
        "Kalshi review and Rule 6.3(b) discretion if value undeterminable"],
    title_vs_rules_divergences=[
        "Market title says 'Pro Baseball Championship' (league name obfuscated) while TITLE.pdf <title> enumerates only NFL, NHL, NBA, NCAA; MLB not named",
        "Series title 'World Series' vs market 'Pro Baseball Championship'",
        "KXMLB-26-* and KXMLB-27-* in one series with 2028 close",
        "KXMLB-26-MIL yes ~0.91 while other 2026 teams remain listed until Source Agency reports elimination"],
    public_feeds_for_settlement_variable=[
        {"name": "MLB Stats API", "url": "https://statsapi.mlb.com/api/v1/schedule?sportId=1", "latency": "seconds", "cost": "free"},
        {"name": "ESPN MLB scoreboard", "url": "https://www.espn.com/mlb/scoreboard", "latency": "seconds-minutes", "cost": "free"},
        {"name": "AP MLB", "url": "https://apnews.com/hub/mlb", "latency": "minutes", "cost": "free"}],
    observability_before_settlement="high",
    toxicity_flags=[
        "game results visible live on public feeds before market reprices (latency race)",
        "insider prohibition (players/staff/owners) with limited enforcement",
        "elimination => immediate No; stale yes quotes picked off",
        "long-dated 2027/2028 strikes illiquid",
        "league name obfuscated in title"],
    notes="Rules from TITLE.pdf. Verify live playoff state before any decision.")
for k, v in cards.items():
    json.dump(v, open(f"data/rules-cards/{k}.json", "w"), indent=1)
