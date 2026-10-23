"""IHP's extra-rules deck (``rule_decks/sg13g2_maximal.drc``) in parts that run at once.

``run_drc.py`` runs the main deck's tables in parallel but the extra rules as one KLayout
process, and KLayout in deep mode keeps about one CPU busy whatever its thread count:
on the 36 MHz SoC's GDS the extra rules ran 2 h 36 min at 4 threads, most of the deck's
wall time. The deck is a header (switches, helpers, reading the layout) ending in
``$start_time = Time.now``, one line per derived layer, then the rules, each a lambda
called at once (``-> do ... end.().output(...)``) or a group of them under ``if
$switch``, and a footer that logs the run's end. A part keeps the header and footer, a
run of the rules in the deck's order, and only the derived layers those rules use with
what they are derived from: KLayout derives a layer when its line runs. Each rule's
block is logged as it starts, so a part's log times its rules.

A deck laid out otherwise is not split (``split`` returns None), and neither is one
whose rules assign a name outside their own block: a part could then miss it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

DECK = "sg13g2_maximal.drc"
START = "$start_time = Time.now"
FOOTER = 'puts("Number of DRC errors'
DONE = "KLayout DRC run for maximum ruleSet completed"  # the footer's log line
_LAYER_RE = re.compile(r"([A-Za-z_]\w*) = (.*)")
_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")
_RULE_RE = re.compile(r"\.output\(\s*\"([^\"]+)\"")
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\+|-|\*|\|\||&&)?=(?![=~])", re.M)
_LOCALS_RE = re.compile(r"\(;([^)]*)\)")
_PLAIN_RULE_RE = re.compile(r"[A-Za-z0-9_.+-]+")


@dataclass(frozen=True)
class Deck:
    header: str
    layers: tuple[tuple[str, str], ...]  # (name, line) for each derived layer, in order
    blocks: tuple[str, ...]  # each rule's block (or a group of them under one switch)
    footer: str


def read(text: str) -> Deck | None:
    """The deck's parts, or None when it is not laid out as ``split`` expects."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == START), None)
    end = next((i for i, ln in enumerate(lines) if ln.startswith(FOOTER)), None)
    if start is None or end is None or end < start:
        return None
    i, layers = start + 1, []
    while i < end and not lines[i].startswith(("-> ", "if ")):
        line = lines[i].rstrip("\n")
        if line.strip():
            m = _LAYER_RE.fullmatch(line)
            if m is None:
                return None
            layers.append((m.group(1), lines[i]))
        i += 1
    blocks = []
    while i < end:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        # A rule's block ends at its "end.()" and a group at its "end", both in column 0.
        if line.startswith("-> "):
            close = next((k for k in range(i + 1, end) if lines[k].startswith("end.()")), None)
        elif line.startswith("if "):
            close = next((k for k in range(i + 1, end) if lines[k].rstrip() == "end"), None)
        else:
            return None
        if close is None:
            return None
        blocks.append("".join(lines[i : close + 1]))
        i = close + 1
    deck = Deck("".join(lines[: start + 1]), tuple(layers), tuple(blocks), "".join(lines[end:]))
    names = {n for n, _ in layers}
    if len(names) != len(layers) or not blocks:
        return None
    # Every name a rule's block assigns must be the block's own (``-> (;x, y) do``).
    for block in blocks:
        own = {v.strip() for m in _LOCALS_RE.finditer(block) for v in m.group(1).split(",")}
        if any(m.group(1) not in own for m in _ASSIGN_RE.finditer(block)):
            return None
    # Each rule reported once, and only by the rules' blocks.
    rules = rules_of("".join(blocks))
    if len(set(rules)) != len(rules) or len(rules) != len(rules_of(text)):
        return None
    return deck


def _part(deck: Deck, blocks: tuple[str, ...]) -> str:
    """The header, the derived layers ``blocks`` use (and theirs), ``blocks`` each logged
    as it starts, and the footer."""
    names = {n for n, _ in deck.layers}
    need = {t for b in blocks for t in _TOKEN_RE.findall(b)} & names
    kept = []
    for name, line in reversed(deck.layers):  # a layer is derived from earlier ones only
        if name in need:
            kept.append(line)
            need |= set(_TOKEN_RE.findall(line.split("=", 1)[1])) & names
    body = []
    for block in blocks:
        rules = [r for r in _RULE_RE.findall(block) if _PLAIN_RULE_RE.fullmatch(r)]
        if rules:
            body.append(f"logger.info('Executing rules {', '.join(rules)}')\n")
        body.append(block)
    return deck.header + "".join(reversed(kept)) + "\n" + "".join(body) + "\n" + deck.footer


def rules_of(text: str) -> list[str]:
    """The rules a deck (or a part of one) reports, in order."""
    return _RULE_RE.findall(text)


# How the deck's switches stand in ChipLeap's runs: recommended rules off (no_recommended),
# fill and latch-up rules on (their defaults).
SWITCHES_ON = frozenset({"$fillerRules", "$latchUpRules"})


def checked(text: str, part: str) -> list[str]:
    """The rules of ``part`` (one of ``split(text)``) that run, and print their counts,
    with the switches as ``SWITCHES_ON`` has them: a rule under any other switch may not."""
    deck = read(text)
    switch = {
        r: b.split("\n", 1)[0][3:].strip()
        for b in (deck.blocks if deck else ())
        if b.startswith("if ")
        for r in rules_of(b)
    }
    return [r for r in rules_of(part) if switch.get(r, "") in SWITCHES_ON or r not in switch]


def split(text: str, parts: int) -> list[str] | None:
    """The deck as ``parts`` decks (fewer when it has fewer rules), each a run of its rules
    in order, the runs as even in rules as they can be; None when it cannot be split."""
    deck = read(text)
    if deck is None or parts < 2:
        return None
    n = min(parts, len(deck.blocks))
    bounds = [round(k * len(deck.blocks) / n) for k in range(n + 1)]
    out = [_part(deck, deck.blocks[a:b]) for a, b in pairwise(bounds)]
    if sorted(r for p in out for r in rules_of(p)) != sorted(rules_of(text)):
        return None
    return out
