"""What YES and NO actually mean for one Kalshi BTC contract.

The bot only trades a contract after this module has *verified* its outcome
condition from Kalshi's own market fields:

* ``strike_type`` (``greater`` / ``greater_or_equal`` / ``less`` / ``less_or_equal``)
  and the matching numeric ``floor_strike`` / ``cap_strike``;
* the published rules text (``rules_primary`` / ``rules_secondary``), which must
  refer to Bitcoin and must not contradict the strike direction;
* the settlement source and any averaging window named in the rules (e.g.
  "the simple average of the sixty seconds of CF Benchmarks' BRTI before close").
  An averaged settlement changes the probability maths, so if the rules mention
  an average but the window cannot be read, the contract is NOT verified.

Range (``between``) and unknown strike types are reported as unsupported rather
than guessed. Anything unverified produces a NO TRADE with the exact problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.kalshi.market_data import MarketInfo

ABOVE_TYPES = {"greater", "greater_or_equal"}
BELOW_TYPES = {"less", "less_or_equal"}
_ASSET_WORDS = re.compile(r"\b(bitcoin|btc|brti|xbt)\b", re.I)
_ABOVE_WORDS = re.compile(r"\b(above|greater|higher|at or above|or more|exceeds?|over)\b", re.I)
_BELOW_WORDS = re.compile(r"\b(below|less than|lower|under|or less)\b", re.I)
_WORD_NUM = {"thirty": 30, "sixty": 60, "ninety": 90, "one minute": 60, "two minutes": 120, "five minutes": 300}


@dataclass(frozen=True)
class ContractTerms:
    ticker: str
    kind: str | None  # "above" | "below" | None (unsupported)
    threshold: float | None  # USD level YES is measured against
    settlement_source: str
    averaging_window_sec: int  # 0 = single print at close
    verified: bool
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def yes_means(self) -> str:
        if self.kind is None or self.threshold is None:
            return "unverified"
        avg = f" ({self.averaging_window_sec}s average)" if self.averaging_window_sec else ""
        word = "ABOVE" if self.kind == "above" else "BELOW"
        return f"BTC settles {word} ${self.threshold:,.2f}{avg} per {self.settlement_source}"

    @property
    def no_means(self) -> str:
        if self.kind is None or self.threshold is None:
            return "unverified"
        word = "AT OR BELOW" if self.kind == "above" else "AT OR ABOVE"
        return f"BTC settles {word} ${self.threshold:,.2f}"

    def direction_for_side(self, side: str) -> str:
        """UP/DOWN meaning of buying ``side`` ("yes"/"no") on this contract."""
        up_side = "yes" if self.kind == "above" else "no"
        return "UP" if side == up_side else "DOWN"

    def side_for_direction(self, direction: str) -> str | None:
        if self.kind is None or direction not in ("UP", "DOWN"):
            return None
        up_side = "yes" if self.kind == "above" else "no"
        return up_side if direction == "UP" else ("no" if up_side == "yes" else "yes")

    @property
    def primary_problem(self) -> str:
        return self.problems[0] if self.problems else ""


def _averaging_window(text: str) -> tuple[int, str | None]:
    """(seconds, problem). 0 when the rules describe no averaging."""
    low = text.lower()
    if "average" not in low:
        return 0, None
    m = re.search(r"average[^.]{0,80}?(\d{1,4})\s*(?:-\s*)?(seconds?|secs?|minutes?|mins?)", low)
    if m:
        n = int(m.group(1))
        return (n * 60 if m.group(2).startswith("min") else n), None
    for words, sec in _WORD_NUM.items():
        if re.search(rf"average[^.]{{0,80}}?\b{words}\b", low):
            return sec, None
    return 0, "settlement uses an average but the averaging window could not be read from the rules"


def _source(text: str) -> str:
    low = text.lower()
    if "brti" in low or "real-time index" in low or "cf benchmarks" in low:
        return "CF Benchmarks BRTI"
    return "Kalshi settlement source (unspecified in rules)"


def interpret_contract(info: MarketInfo, *, require_rules: bool = True,
                       assumed_avg_window_sec: int | None = None) -> ContractTerms:
    """Interpret and verify one market. ``require_rules=False`` is for research on old cached data only."""
    problems: list[str] = []
    notes: list[str] = []
    rules = f"{info.rules_primary} {info.rules_secondary}".strip()
    st = (info.strike_type or "").lower()

    if info.asset != "BTC":
        problems.append(f"not a Bitcoin market ({info.asset})")
    kind: str | None = None
    threshold: float | None = None
    if st in ABOVE_TYPES:
        kind, threshold = "above", info.floor_strike
    elif st in BELOW_TYPES:
        kind, threshold = "below", info.cap_strike
    elif st == "between":
        problems.append("range (between) contracts are not supported yet")
    else:
        problems.append(f"unknown strike type {info.strike_type!r}")
    if kind is not None and (threshold is None or threshold <= 0):
        problems.append("threshold (strike) not published for this market")
        kind = None

    window = 0
    if rules:
        if not _ASSET_WORDS.search(rules):
            problems.append("rules text does not refer to Bitcoin")
        has_up, has_down = bool(_ABOVE_WORDS.search(rules)), bool(_BELOW_WORDS.search(rules))
        if kind == "above" and has_down and not has_up:
            problems.append("rules text describes a BELOW condition but strike type says ABOVE")
        if kind == "below" and has_up and not has_down:
            problems.append("rules text describes an ABOVE condition but strike type says BELOW")
        window, wproblem = _averaging_window(rules)
        if wproblem:
            problems.append(wproblem)
        source = _source(rules)
    elif require_rules:
        problems.append("settlement rules text missing - cannot verify what YES means")
        source = "unknown"
    else:
        source = "assumed (rules text not cached)"
        window = assumed_avg_window_sec or 0
        notes.append("rules text unavailable; research assumption used")

    return ContractTerms(
        ticker=info.ticker,
        kind=kind,
        threshold=threshold if kind else None,
        settlement_source=source,
        averaging_window_sec=window,
        verified=not problems and kind is not None,
        problems=tuple(problems),
        notes=tuple(notes),
    )
