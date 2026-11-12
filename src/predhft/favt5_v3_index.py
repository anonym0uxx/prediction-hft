"""FavT5 v3 offline research scorer. Reads one JSON snapshot; emits a paper intent or null."""
import json
import math
import sys
from decimal import Decimal, ROUND_CEILING

BUDGET = Decimal("10.00")
TAKER = Decimal(".07")
CENT = Decimal(".01")

def t5_cdf(x):
    angle = math.atan(x / math.sqrt(5.0))
    return 0.5 + (angle + 2 * math.sin(2 * angle) / 3 + math.sin(4 * angle) / 12) / math.pi

def index_features(ticks, decision, lag):
    """Require past-only one-second changes. A missing second breaks that return."""
    available = {}
    for e in ticks:
        event, receive, value = e["event"], e["received"], e["value"]
        if not all(map(math.isfinite, (event, receive, value))) or value <= 0:
            continue
        second = math.floor(event)
        if decision-610 <= second and max(event, receive) <= decision-lag:
            previous = available.get(second)
            if previous is None or (event, receive) < previous[:2]:
                available[second] = (event, receive, value)
    if not available:
        return None
    last = max(available)
    if decision-last > lag+4:
        return None
    changes = []
    for i in range(last-599, last+1):
        if i in available and i-1 in available:
            changes.append((i, (available[i][2]-available[i-1][2])**2))
    fast = [v for i,v in changes if i > last-120]
    if len(fast) < 100 or len(changes) < 460:
        return None
    rv = math.sqrt((sum(fast)/len(fast) + sum(v for _,v in changes)/len(changes))/2)
    return last, available[last][2], rv

def select(fair_yes, bid, ask):
    if not (0 < bid <= ask < 1 and ask-bid <= .06):
        return None
    strongest = None
    for side, probability, offer in (("YES", fair_yes, ask), ("NO", 1-fair_yes, 1-bid)):
        offer = Decimal(str(round(offer,4)))
        if not Decimal(".15") <= offer <= Decimal(".60"):
            continue
        limit = offer + CENT
        unit_cost = limit + TAKER * limit * (1-limit)
        qty = int(BUDGET / unit_cost)
        cost = (qty * unit_cost).quantize(CENT, rounding=ROUND_CEILING)
        edge = probability * qty - float(cost)
        if qty and cost <= BUDGET and edge >= .05 * qty:
            if strongest is None or edge > strongest["modeled_net_dollars"]:
                strongest = dict(side=side, qty=qty, limit=float(limit), max_total_cost=float(cost),
                                 modeled_probability=probability, modeled_net_dollars=edge)
    return strongest

def signal(snapshot):
    """Caller supplies a single as-of top-of-book and official strike already published for this ticker."""
    if snapshot["series"] != "KXBTC15M":
        return None
    end = snapshot["close"]
    decision = end - 480
    book = snapshot["quote"]
    stamps = (book["event"],book["received"],snapshot["strike_received"])
    if any(not math.isfinite(t) or t > decision for t in stamps) or decision-book["event"] > 3:
        return None
    strike = snapshot["official_strike"]
    quoted_strike = snapshot["quoted_strike"]
    if not (math.isfinite(strike) and math.isfinite(quoted_strike) and strike > 0 and quoted_strike > 0
            and abs(quoted_strike-strike) <= .02):
        return None
    ticks = snapshot["brti"]
    slow = index_features(ticks, decision, 3)
    if slow is None or index_features(ticks, decision, 1) is None:
        return None
    last, value, rv = slow
    time_variance = max(0, end-60-last) + 59*119/(6*60)
    sigma = math.hypot(max(rv, value*.000012)*math.sqrt(time_variance), quoted_strike*.000035)
    market_mid = (book["bid"]+book["ask"])/2
    fair_yes = .75*t5_cdf((value-quoted_strike)/sigma) + .25*market_mid
    return select(fair_yes, book["bid"], book["ask"])

if __name__ == "__main__":
    print(json.dumps(signal(json.load(sys.stdin)), allow_nan=False))
