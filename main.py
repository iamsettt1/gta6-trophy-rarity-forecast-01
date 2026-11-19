"""GTA VI trophy-rarity forecast (make.fun gta6-trophy-rarity-forecast-01).

Model (my own composition; see MODEL.md for the data and calibration):
  rate_i(t) = E_i(t) / N(t).  Earners and tracked players are modelled
  separately, because at launch the tracked population floods with brand-new
  players (who have earned ~nothing yet) while earners catch up only as the
  cohort plays.  That dilution-vs-progression split is what persistence
  misses: the 04:30Z refresh took every rate down even though earners grew.

  * N(t), players tracked: fitted from the case history's distinct page
    states (log-linear, growth decaying with a 96 h time constant).  When the
    history is a single stale page state (the practice cases), the documented
    launch-night refresh is used instead: the page jumps ~6.06x at its first
    refresh (638 -> 3,868 tracked), with the tracked population growing
    exponentially at 0.30/h underneath.  A page state's true time is when
    the tracked curve actually equalled the page value, not when the house
    first read it: for a single stale state that is
    t_target - ln(6.06)/0.30 hours.
  * E_i(t): a convolution.  Players join at rate J(u) = dN/du; a joiner earns
    trophy i after a lognormal delay (median per trophy TYPE, sigma 0.8 in
    log space) with eventual share s_inf (mature share, calibrated on Red
    Dead Redemption 2's Exophase rates).  Per trophy, a delay multiplier and
    a share factor are fitted by penalised least squares to that trophy's
    own earners across the history's states, so the growth rate each trophy
    shows decides its delay.  A constant offset carries the early-access
    cohort (the handful of players who had finished the game before launch)
    in the small-N regime; it is not launch-population growth.
  * Quantiles: median = forecast rate; logit spread from type uncertainty,
    horizon and (for a stale single-state history) the chance the refresh
    does not land before the target, which widens the upper tail only.

The program never crashes: any failure in a case falls back to a valid
persistence forecast, so the output is always a legal file.

Standard library only, no network, deterministic.
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]

# --- calibrated constants (sources in MODEL.md) ---------------------------
LAUNCH_OFFSET_HOURS = 13.0   # NZ/AU midnight launch precedes the nominal
                             # release date by ~13 h; first reads already
                             # show 638 tracked with trophies earned.
R_PRIOR = 0.30               # launch-night tracked growth, per hour.
REFRESH_FACTOR = 6.06        # 638 -> 3,868 tracked at the first refresh.
SIG_DELAY = 0.6              # lognormal delay spread (log hours); 0.6 fits
                             # the launch anchors better than 0.8: a wider
                             # spread puts too many earners in the first hours
                             # (no launch player finishes 250 stunts by 17 h).
TAU_N = 288.0                # decay time of tracked-growth rate, hours.
N_CAP = 500000.0             # sanity cap on tracked players (GTA V on
                             # Exophase tracks ~236k after 13 years).
R_MAX = 0.02                 # cap on fitted tracked growth, per hour.
SMALL_N = 5000.0             # below this, the early-access cohort matters.

# (median delay hours, mature share, logit spread) per trophy type.
# Medians fitted to the launch-night anchors at SIG_DELAY = 0.6, r = 0.30/h
# (see MODEL.md); shares from Red Dead Redemption 2's mature Exophase rates.
TYPES = {
    "intro":    (0.2, 1.000, 0.10),
    "ch1":      (33.0, 0.83, 0.45),
    "ch2":      (47.0, 0.61, 0.55),
    "ch3":      (47.0, 0.53, 0.55),
    "ch4":      (44.0, 0.49, 0.55),
    "final":    (44.0, 0.44, 0.55),
    "mission":  (47.0, 0.55, 0.55),
    "stranger": (50.0, 0.35, 0.60),
    "strands8": (35.0, 0.20, 0.60),
    "easy":     (44.0, 0.40, 0.60),
    "quick":    (21.0, 0.35, 0.60),
    "race":     (120.0, 0.25, 0.65),
    "grind":    (26.0, 0.12, 0.75),
    "money":    (31.0, 0.25, 0.65),
    "platinum": (772.0, 0.027, 0.80),
    "hundred":  (772.0, 0.040, 0.80),
}

# Grid for the per-trophy delay multiplier and share factor.
LAMBDAS = (0.25, 0.35, 0.5, 0.63, 0.8, 1.0, 1.25, 1.6, 2.0, 2.5, 3.2, 4.0)
SHARE_FACTORS = (0.5, 0.7, 1.0, 1.4, 2.0)


def classify(trophy):
    """Trophy type from its own description and tier (never from its id)."""
    desc = (trophy.get("description") or "").lower()
    tier = (trophy.get("tier") or "").lower()
    if tier == "platinum":
        return "platinum"
    if "gone fishing" in desc:
        return "intro"
    if "100% completion" in desc:
        return "hundred"
    if "complete chapter 1" in desc:
        return "ch1"
    if "complete chapter 2" in desc:
        return "ch2"
    if "complete chapter 3" in desc:
        return "ch3"
    if "complete chapter 4" in desc:
        return "ch4"
    if "complete the final mission for" in desc:
        return "stranger"
    if "complete the final mission" in desc:
        return "final"
    if "8 mission strands" in desc:
        return "strands8"
    if "fishing challenge" in desc or "arcade game" in desc:
        return "quick"
    if "races" in desc or "drone race" in desc or "each type of activity" in desc:
        return "race"
    if ("stunts" in desc or "shooting range" in desc or "collectible card" in desc
            or "waink" in desc or "50 challenges" in desc or "bank vaults" in desc
            or "100 vehicles" in desc or "meditate" in desc or "explore all" in desc):
        return "grind"
    if ("laundering" in desc or "spend $" in desc or "garage properties" in desc
            or "register 10 vehicles" in desc):
        return "money"
    if desc.startswith("complete "):
        return "mission"
    return "easy"


def parse_time(value):
    """ISO 8601 (with Z) or plain date -> aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def clamp_rate(x):
    return min(99.99, max(0.01, float(x)))


def logit(pct):
    p = clamp_rate(pct)
    return math.log(p / (100.0 - p))


def inv_logit(lg):
    if lg > 20.0:
        return 99.99
    if lg < -20.0:
        return 0.01
    return 100.0 / (1.0 + math.exp(-lg))


def round4(x):
    scaled = x * 10000.0
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 10000.0
    return int(value) if float(value).is_integer() else value


def lognorm_cdf(hours, median):
    """P(delay <= hours) for a lognormal delay with median `median`."""
    if hours <= 0.0:
        return 0.0
    z = (math.log(hours) - math.log(median)) / SIG_DELAY
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def distinct_states(history, launch):
    """Collapse consecutive identical page reads into states.

    Returns [(t_hours, players_tracked, [rates...])] with t measured from
    launch; a state's time is its first read (the page was set at or before
    then).  Reads are sorted by time first, so order is deterministic.
    """
    reads = []
    for entry in history or []:
        at = parse_time(entry.get("at")) or parse_time(entry.get("slot"))
        if at is None:
            continue
        n = entry.get("players_tracked")
        rates = entry.get("rates")
        if not isinstance(n, (int, float)) or n <= 0 or not isinstance(rates, list):
            continue
        reads.append((at, float(n), [r if isinstance(r, (int, float)) else None
                                     for r in rates]))
    reads.sort(key=lambda item: item[0])
    states = []
    for at, n, rates in reads:
        t = max(0.0, (at - launch).total_seconds() / 3600.0)
        if states and states[-1][1] == n and states[-1][2] == rates:
            continue
        states.append((t, n, rates))
    return states


class TrackedModel:
    """The tracked-population curve N(t) and its hourly join flow J(u)."""

    def __init__(self, states, t_target):
        self.mode = "multi" if len(states) > 1 else "single"
        self.t_target = t_target
        if not states:
            self.n_target = 638.0 * math.exp(R_PRIOR * t_target)
            self.t_cal = 0.0
            self._n_at = lambda t: 638.0 * math.exp(R_PRIOR * t)
        elif len(states) == 1:
            t_state, n_page, _ = states[0]
            # The refresh reveals REFRESH_FACTOR times the stale page value.
            self.n_target = n_page * REFRESH_FACTOR
            # The state was set when the true curve equalled the page value.
            self.t_cal = t_target - math.log(REFRESH_FACTOR) / R_PRIOR
            n_t = self.n_target
            self._n_at = lambda t: n_t * math.exp(R_PRIOR * (t - t_target))
        else:
            # Slope from RECENT states only (last 24 h, at least 3 states):
            # the early launch surge must not set the extrapolation rate.
            recent = [s for s in states if s[0] >= states[-1][0] - 24.0]
            if len(recent) < 3:
                recent = states[-3:]
            ts = [s[0] for s in recent]
            lns = [math.log(max(1.0, s[1])) for s in recent]
            npts = len(ts)
            mean_t = sum(ts) / npts
            mean_ln = sum(lns) / npts
            denom = sum((t - mean_t) ** 2 for t in ts)
            slope = 0.0
            if denom > 0:
                slope = sum((t - mean_t) * (l - mean_ln)
                            for t, l in zip(ts, lns)) / denom
            slope = min(max(slope, 0.0), R_MAX)
            self._slope = slope
            self._t_last, self._n_last = states[-1][0], states[-1][1]
            self._segments = []
            for (t0, n0, _), (t1, n1, _) in zip(states, states[1:]):
                if t1 > t0:
                    self._segments.append((t0, math.log(n0), t1, math.log(n1)))
            self.n_target = self._n_at_multi(t_target)
            self.t_cal = states[-1][0]
        # Hourly join flow on a 1 h grid from 0 to t_target.
        self.n_grid_t = t_target
        steps = int(math.ceil(t_target)) + 1
        self.join = []
        prev = self.n_at(0.0)
        for k in range(1, steps + 1):
            cur = self.n_at(float(k))
            self.join.append(max(0.0, cur - prev))
            prev = cur
        self._cum_cache = {}

    def _n_at_multi(self, t):
        segs = self._segments
        if not segs:
            return self._n_last
        if t <= segs[0][0]:
            t0, l0, t1, l1 = segs[0]
            r0 = (l1 - l0) / (t1 - t0) if t1 > t0 else 0.0
            return math.exp(l0 + r0 * (t - t0))
        for t0, l0, t1, l1 in segs:
            if t0 <= t <= t1:
                frac = (t - t0) / (t1 - t0)
                return math.exp(l0 + frac * (l1 - l0))
        h = t - self._t_last
        growth = self._slope * TAU_N * (1.0 - math.exp(-h / TAU_N))
        return min(N_CAP, self._n_last * math.exp(growth))

    def n_at(self, t):
        if hasattr(self, "_segments"):
            return self._n_at_multi(t)
        return self._n_at(t)

    def cum_unit(self, t_end, median):
        """Convolution with s_inf = 1: sum_u J(u) * CDF(t_end - u)."""
        key = (round(median, 6), round(t_end, 3))
        hit = self._cum_cache.get(key)
        if hit is not None:
            return hit
        total = 0.0
        # Hourly cells: cell k covers [k, k+1), midpoint k + 0.5.
        limit = int(math.floor(t_end))
        for k in range(limit):
            if k >= len(self.join):
                break
            total += self.join[k] * lognorm_cdf(t_end - (k + 0.5), median)
        self._cum_cache[key] = total
        return total


def early_offset(kind, earners_last, earners_final):
    """Early-access cohort earners carried inside the observed count.

    Only in the small-N regime: the handful of players who had finished the
    game before launch.  Their count is read off the final-mission trophy's
    own earners at the last state.
    """
    if kind in ("platinum", "hundred"):
        return earners_last
    if kind in ("grind", "money", "easy"):
        # In the launch window these show ~no launch-population growth in
        # the anchors (e.g. 250 Stunts 4 -> 4 earners, Bank Vaults 6 -> 5):
        # the earners at the first read are the early-access cohort.
        return earners_last
    return min(earners_last, earners_final)


def fit_trophy(kind, observations, offset, model):
    """Fit (delay multiplier, share factor, constant offset) to one trophy.

    observations: [(t_hours, earners)] at the history's states (launch
    population only: offset already subtracted).  Penalised least squares
    in log space; both penalties are stiff, so with a single observation
    the fit stays near the type prior and only a large level surprise
    moves the delay (the delay is what sets the growth rate, and a fast
    delay fitted to one level would explode the forecast).  The constant
    offset carries earners that do not grow with the launch population
    (the early-access cohort: a trophy whose earners sit flat while the
    tracked population multiplies is a constant, not growth); it costs a
    fixed penalty so it only wins when the trajectory really is flat.
    Delay and share are partly collinear in-sample but diverge out of
    sample, so the forecast averages the target earners over every grid
    point within 1.5 of the best penalised error instead of trusting one
    optimum.  Returns (target earners, best median, best share, best
    total offset).
    """
    median0, s_inf0, _ = TYPES[kind]
    first_earn = observations[0][1] if observations else 0.0
    c_options = (0.0, 0.5 * first_earn, first_earn)
    scored = []
    for lam in LAMBDAS:
        median = median0 * lam
        cums = [model.cum_unit(t, median) for t, _ in observations]
        cum_target = model.cum_unit(model.t_target, median)
        for factor in SHARE_FACTORS:
            s_inf = s_inf0 * factor
            for const in c_options:
                err = 0.0
                for (t, obs), cum in zip(observations, cums):
                    pred = const + s_inf * cum
                    err += (math.log(pred + 0.5) - math.log(obs + 0.5)) ** 2
                err += 2.0 * math.log(lam) ** 2
                err += 2.0 * math.log(factor) ** 2
                if const > 0:
                    err += 1.0
                scored.append((err, offset + const + s_inf * cum_target,
                               median, s_inf, offset + const))
    scored.sort(key=lambda row: row[0])
    best_err = scored[0][0]
    near = [row for row in scored if row[0] <= best_err + 1.5]
    e_target = sum(row[1] for row in near) / len(near)
    _, _, median_b, s_inf_b, offset_b = scored[0]
    return e_target, median_b, s_inf_b, offset_b


def forecast_case(case):
    trophies = case.get("trophies") or []
    n_tr = len(trophies)
    if n_tr == 0:
        return {"trophies": []}
    game = case.get("game") or {}
    release = parse_time(game.get("release_date"))
    if release is None:
        release = parse_time(case.get("at"))
    if release is None:
        release = datetime(2026, 11, 19, tzinfo=timezone.utc)
    launch = release - timedelta(hours=LAUNCH_OFFSET_HOURS)
    target_dt = parse_time(case.get("target"))
    at_dt = parse_time(case.get("at"))
    horizon_h = float(case.get("horizon_minutes") or 0) / 60.0
    if target_dt is not None:
        t_target = max(0.0, (target_dt - launch).total_seconds() / 3600.0)
    elif at_dt is not None:
        t_target = max(0.0, (at_dt - launch).total_seconds() / 3600.0) + horizon_h
    else:
        t_target = horizon_h
    if horizon_h <= 0 and target_dt is not None and at_dt is not None:
        horizon_h = max(0.0, (target_dt - at_dt).total_seconds() / 3600.0)

    states = distinct_states(case.get("history") or [], launch)
    model = TrackedModel(states, t_target)
    n_target = model.n_target
    t_cal = model.t_cal
    days = max(horizon_h, 0.0) / 24.0

    # Last non-null rate per trophy, and the final-mission trophy's earners
    # (the early-access cohort's size) at the last state.
    rates_last = [None] * n_tr
    for _, n_state, rates in states:
        for i in range(min(n_tr, len(rates))):
            if rates[i] is not None:
                rates_last[i] = float(rates[i])
    n_last_state = states[-1][1] if states else 638.0
    earners_final = 0.0
    for i, trophy in enumerate(trophies):
        if classify(trophy) == "final" and rates_last[i] is not None:
            earners_final = rates_last[i] * n_last_state / 100.0
    small_n = bool(states) and n_last_state < SMALL_N and len(states) == 1

    # Earners per state per trophy (for the fit), in state-time order.
    # In the single-state regime the state is calibrated at t_cal, not at
    # its first-read time.
    state_times = []
    for idx, (t_s, n_s, _) in enumerate(states):
        state_times.append(t_cal if (len(states) == 1 and idx == 0) else t_s)

    rows = []
    for i, trophy in enumerate(trophies):
        kind = classify(trophy)
        median0, s_inf0, sig_type = TYPES[kind]
        rate_last = rates_last[i]
        if rate_last is None:
            rate_last = 0.0
        earners_last = rate_last * n_last_state / 100.0

        # Intro trophy that every tracked player holds stays saturated.
        if rate_last >= 99.5:
            med = 99.9
            sig_lo = sig_up = 0.15
        else:
            offset = early_offset(kind, earners_last, earners_final) if small_n else 0.0
            observations = []
            for (t_s, n_s, rates), t_use in zip(states, state_times):
                if i < len(rates) and rates[i] is not None:
                    obs = max(0.0, float(rates[i]) * n_s / 100.0 - offset)
                    observations.append((t_use, obs))
            if not observations:
                observations = [(t_cal, 0.0)]
            e_target, _, _, _ = fit_trophy(kind, observations, offset, model)
            earners_target = e_target
            med = clamp_rate(earners_target / max(1.0, n_target) * 100.0)
            sig_lo = math.sqrt(0.45 ** 2 + sig_type ** 2
                               + (0.12 * math.sqrt(days)) ** 2
                               + (0.30 if earners_last < 5 else 0.0) ** 2)
            sig_up = sig_lo
            if model.mode == "single":
                # The refresh may not land before the target: widen the
                # upper tail toward the no-refresh (undiluted) rate.
                sig_up = math.sqrt(sig_lo ** 2 + 0.9 ** 2)
        med_lg = logit(med)
        qs = []
        for z in Z:
            sig = sig_up if z > 0 else sig_lo
            qs.append(round4(inv_logit(med_lg + z * sig)))
        for k in range(1, len(qs)):
            if qs[k] < qs[k - 1]:
                qs[k] = qs[k - 1]
        rows.append({"id": trophy["id"], "q": qs})
    return {"trophies": rows}


def fallback_forecast(case):
    """Valid persistence forecast, used only if the model fails a case."""
    rows = []
    trophies = case.get("trophies") or []
    rates = {}
    for entry in case.get("history") or []:
        for i, r in enumerate(entry.get("rates") or []):
            if r is not None and i < len(trophies):
                rates[i] = float(r)
    spread = 0.5 * math.sqrt(max(0.0, float(case.get("horizon_minutes") or 0)) / 1440.0)
    for i, trophy in enumerate(trophies):
        r = rates.get(i, 1.0)
        lg = logit(r)
        qs = [round4(inv_logit(lg + z * spread)) for z in Z]
        for k in range(1, len(qs)):
            if qs[k] < qs[k - 1]:
                qs[k] = qs[k - 1]
        rows.append({"id": trophy["id"], "q": qs})
    return {"trophies": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
            forecast = forecast_case(case)
            if (not isinstance(forecast, dict) or set(forecast) != {"trophies"}
                    or len(forecast["trophies"]) != len(case.get("trophies") or [])):
                raise ValueError("bad forecast shape")
            for row in forecast["trophies"]:
                q = row["q"]
                if (len(q) != 5 or any(not isinstance(v, (int, float))
                                       or v != v or v in (float("inf"), float("-inf"))
                                       or v < 0 or v > 100 for v in q)
                        or any(q[k] < q[k - 1] for k in range(1, 5))):
                    raise ValueError("bad quantiles")
        except Exception:
            try:
                case = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                case = {"trophies": [], "history": [], "horizon_minutes": 0}
            forecast = fallback_forecast(case)
        text = json.dumps(forecast, separators=(",", ":")) + "\n"
        (OUTPUT_DIR / f"{path.stem}.out").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
