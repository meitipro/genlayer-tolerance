# Tolerance — specification

Purpose, consensus, state, API, reuse. Written so a reviewer can judge the
design without opening the source, and so a builder can decide whether to lift
it without reading the tests.

---

**File:** [`contracts/tolerance.py`](contracts/tolerance.py) · 137 tests
**Runner:** `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`

### Purpose

Pull several numbers off a web page, where each number declares on chain how
closely validators must agree about it, and how far from believable it is allowed
to be.

This is the most common failure in Intelligent Contracts. Somebody reads a number
off a page and wraps it in `strict_eq`. It works in Studio with one validator and
then never reaches consensus live, because two nodes fetch the page four seconds
apart and one sees 1,204 and the other 1,205.

The reflex fix is to loosen everything to a prompt-based principle, which then
also accepts 12,040. Both failures come from the same mistake: applying **one**
agreement rule to numbers with completely different stability. A published fee
percentage should match exactly. A live counter should not.

### Consensus

`gl.vm.run_nondet_unsafe`. One prompt extracts every field. The validator runs
its own read and compares **field by field**, each against its own frozen rule:

| Mode | Meaning |
|---|---|
| `exact` | identical to within floating point noise |
| `abs:X` | may differ by at most X in absolute terms |
| `pct:X` | may differ by at most X percent of the larger value |
| `band:a,b,c` | must fall in the same declared bucket |

```python
for name, mode, param in specs:
    if not within(mode, param, mine.get(name), theirs.get(name)):
        return False
```

One volatile field does not loosen the rule for a stable one, and one stable
field does not break consensus over a volatile one.

`band` is the strongest of the four and the one most people should reach for. It
collapses a noisy number into a bucket *before consensus ever sees it*, which is
the general trick for making any noisy output agreeable.

`within()` is **symmetric by construction** — an asymmetric agreement rule would
make consensus depend on who happened to be elected leader, which is a subtle and
very unpleasant bug. A test asserts symmetry across every mode.

Both nodes must also agree on whether the page was **readable at all**. An
unreachable page is a fact about the page, not about the numbers, and it is
returned as a value rather than raised — raising would fail the transaction with
"validators did not agree" at exactly the moment both nodes agreed perfectly.

### The second rule: a plausibility guard

Tolerance says how closely two validators must agree. It says nothing about
whether the number was ever believable. So every field carries a second,
independent rule, enforced deterministically **after** consensus:

| Guard | Meaning |
|---|---|
| `step:400` | may not move more than 400 from the last **accepted** reading |
| `range:0,100` | must always fall between 0 and 100 |
| `step:400;range:0,100` | both |

Two guards rather than one, because they catch different lies. A step bound
catches a value that drifted somewhere impossible. A range bound catches a value
that was never possible — including on the **first** reading, where there is no
previous point and a step bound can say nothing at all.

The baseline is the last **accepted** reading, not the last reading. Taking the
last reading would compare an absurd value against nothing, and the next absurd
value would sail straight through.

A value that passes validator agreement and fails its guard is stored and marked
**not accepted**, with the reason. `value()` then reports `present: false`.

### State

| Field | Shape | Note |
|---|---|---|
| `meters` | `DynArray[Meter]` | append-only |
| `Meter.url` | `str` | **frozen at definition** |
| `Meter.fields` | `DynArray[FieldSpec]` | name, mode, param, step, range — all frozen |
| `Meter.readings` | `DynArray[Reading]` | every reading, accepted or not |
| `Value.number` | `str` | stored as text so no precision is lost on the way in |
| `Reading.rejected` | `str` | `""` when accepted, otherwise why the guard refused |

Tolerances and guards are validated at `define()` rather than at read time, so a
meter that could never reach consensus is refused before anybody depends on it.

**No float crosses the calldata boundary.** A consuming contract cannot do
reliable float arithmetic on chain, so `value()` hands back the canonical decimal
string plus a fixed point integer at scale 1,000,000.

Infinity and NaN are refused at every entry point. `float("1e400")` is infinity,
and infinity passes every `<=` range check ever written; NaN compares false
against itself, so a NaN tolerance would create a field that can never reach
consensus, silently, forever.

### API

```python
define(label, url, names: str, tolerances: str, guards: str)   # freeze the meter
read(meter_id: u256)                               # extract, agree, guard, store

value(meter_id, field) -> dict   # {present, number: str, scaled: int, scale: int}
latest(meter_id)       -> dict   # the reading, accepted or not, with the reason
meter(meter_id)        -> dict   # the rules, published so a value can be argued with
count()                -> u256
```

### Reuse

Any price, count, rate, limit, threshold or percentage a contract currently reads
from a page belongs in one of the four modes. Price feeds, quota and limit
monitors, treasury reporting, uptime counters, supply figures.

The comparison function is pure and copyable on its own — see
[`lib/tolerance_consensus.py`](lib/tolerance_consensus.py).
