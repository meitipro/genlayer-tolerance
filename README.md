<p align="left"><img src="brand/lockup.svg" alt="tolerance" height="64"></p>

# Tolerance — per-field numeric agreement and plausibility guards

A reusable primitive for pulling several numbers out of a web page, where each number declares **on chain** how closely validators must agree about it.

- **Contract:** [`contracts/tolerance.py`](contracts/tolerance.py)
- **Tests:** `pytest tests/ -q` → **151 passed**, nothing to install but pytest
- **Deployed:** `{address}` on studionet ([explorer](https://explorer-studio.genlayer.com/address/{address}))
- **Specification:** [CONTRACTS.md](CONTRACTS.md)
- **Decisions and limits:** [DECISIONS.md](DECISIONS.md)
- **License:** MIT. Copy the agreement rule; that is what it is for.

---

## The problem

This is the single most common mistake in Intelligent Contracts. Somebody
reads a number off a page and wraps it in `strict_eq`. It works in Studio
with one validator and then never reaches consensus on a real network,
because two nodes fetch the page four seconds apart and one of them sees
1,204 and the other sees 1,205.

The reflex fix is to loosen everything to a prompt-based principle, which
then also accepts 12,040. Both failures come from the same mistake: applying
**one** agreement rule to numbers that have completely different stability.
A published fee percentage should match exactly. A live counter should not.

## How consensus is used

Every field carries its own tolerance, chosen when the meter was defined and
frozen there:

```
exact        identical to within floating point noise
abs:X        may differ by at most X in absolute terms
pct:X        may differ by at most X percent of the larger value
band:a,b,c   must fall in the same declared bucket
```

The validator runs its own read and compares **field by field**, each against
its own rule:

```python
for name, mode, param in specs:
    if not within(mode, param, mine.get(name), theirs.get(name)):
        return False
```

One volatile field does not loosen the rule for a stable one, and one stable
field does not break consensus over a volatile one.

`band` is the strongest of the four. It collapses a noisy number into a
bucket *before consensus ever sees it*, which is the general trick for making
any noisy output agreeable.

### The second rule: a plausibility guard

Tolerance says how closely two validators must agree. It says nothing about
whether the number was ever believable. So every field carries a second,
independent rule, enforced deterministically **after** consensus:

```
step:400              may not move more than 400 from the last ACCEPTED reading
range:0,100           must always fall between 0 and 100
step:400;range:0,100  both
```

Two guards rather than one, because they catch different lies. A step bound
catches a value that drifted somewhere impossible. A range bound catches a
value that was never possible — including on the **first** reading, where
there is no previous point and a step bound can say nothing at all.

The baseline is the last **accepted** reading, not the last reading. Taking
the last reading would compare an absurd value against nothing, and the next
absurd value would sail straight through.

A value that passes validator agreement and fails its guard is stored and
marked **not accepted**, with the reason. **Validators agreeing on an absurd
number does not make it true.**

## Why this is not a thin LLM wrapper

The model extracts; it never decides. Which numbers are acceptable, how far
apart two nodes may be, and how far a value may move between readings are all
declared on chain before any model runs, and all enforced in Python.

Infinity and NaN are refused at every entry point. `float("1e400")` is
infinity, and infinity passes every `<=` range check ever written; NaN
compares false against itself, so a NaN tolerance would create a field that
can never reach consensus, silently, forever.

---

## The API

```python
define(label, url, names, tolerances, guards)   # three pipe separated strings
read(meter_id)                                  # extract, agree, guard, store

value(meter_id, field) -> dict   # {present, number, scaled, scale}
latest(meter_id)       -> dict   # the reading and whether it was accepted
meter(meter_id)        -> dict   # the rules, published
count()                -> int
```

Tolerances and guards are validated at `define()` rather than at read time,
so a meter that could never reach consensus is refused before anybody depends
on it.

## Using it from another contract

```python
@gl.contract_interface
class Tolerance:
    class View:
        def value(self, meter_id: int, field: str) -> dict: ...



# in a consuming contract
v = Tolerance(TOLERANCE_ADDR).view().value(mid, "fee_pct")
if v["present"] and v["scaled"] > 500_000:   # 0.5, at scale 1e6
    self._pause()
```

**No float crosses the calldata boundary.** A consuming contract cannot do
reliable float arithmetic on chain, so it gets a fixed point integer it can
actually compare, plus the exact decimal string for anything it displays.

`present` is false for a missing field, a rejected reading, an unreadable
page, and a meter never read — one safe branch instead of four.

---

## Running the tests

```bash
pip install pytest
pytest tests/ -q
```

```
151 passed, 1 skipped
```

Three suites, covering different things.

**`tests/test_logic.py`** — the pure agreement rules, exhaustively. They are
module-level functions in the contract, so this file reads the **real contract
source** and executes the helper section with a stub for `genlayer`. There is no
second copy of the logic to drift out of sync.

**`tests/test_e2e.py`** — the contract itself, executed. It runs on
[`tests/glsim.py`](tests/glsim.py), a small GenVM stand-in included here, so it
needs no Studio and no network. This is what reaches the deterministic half:
storage round-trips, the post-consensus re-derivation, and every branch that
only fires when the leader and a validator see different things.

The important part is that the leader and the validator get **their own** mock
pages and prompt answers, so a contract that quietly assumes both nodes see
identical bytes fails here rather than on a real network.

**`tests/test_integration.py`** — against a real Studio, skipped automatically
when `genlayer-test` is not installed:

```bash
pip install genlayer-test
gltest --network studionet tests/test_integration.py
```

### The tests have teeth

Passing tests prove nothing on their own, so every safety property was broken on
purpose to confirm a test notices. Nineteen mutations were introduced against
this contract and all nineteen were caught:

| Mutation | Caught by |
|---|---|
| the baseline no longer walking back to the last accepted reading | `test_a_second_absurd_reading_is_still_rejected` |
| the range guard dropped, leaving only the step guard | `test_a_range_guard_catches_the_very_first_reading` |
| the readability agreement check removed | `test_nodes_disagreeing_about_readability_do_not_agree` |
| every field sharing one loose tolerance | `test_a_strict_field_may_not_drift_at_all` |
| a value missing on one side only forgiven | `test_a_field_found_by_one_node_only_is_a_disagreement` |
| infinity accepted as a number | `test_infinity_and_nan_from_the_model_are_read_as_absent` |
| non-finite tolerance parameters accepted | `test_non_finite_tolerances_are_refused` |
| non-finite guard parameters accepted | `test_non_finite_guards_are_refused` |
| floats sent across the calldata boundary | `test_no_float_crosses_the_calldata_boundary` |
| unexpected keys accepted in a proposal | `test_a_fabricated_extra_field_is_rejected` |
| the view bounds check removed | `test_a_read_with_a_nonexistent_id_is_a_user_error` |
| negative ids allowed through to Python list indexing | `test_a_read_with_a_negative_id_does_not_return_the_last_record` |
| a lookup that ignores the meter id | `test_two_meters_do_not_read_each_other_s_fields_or_readings` |
| a lookup that ignores the reading id | `test_two_meters_do_not_read_each_other_s_fields_or_readings` |
| a collection nested back inside a storage dataclass | `test_no_storage_dataclass_holds_a_collection` |
| an `int` storage field | `test_no_forbidden_storage_types` |
| a ghost field that never persists | `test_every_persistent_field_is_declared_in_the_class_body` |
| a live storage object passed into the block | `test_no_block_closes_over_a_storage_object` |
| a storage field declared twice | `test_no_storage_field_is_declared_twice` |

---

## Deploying

```bash
npm i -g genlayer
./scripts/deploy.sh studionet
```

`deploy.sh` lints, deploys, and then **exercises** the contract, so the explorer
page shows real method calls with consensus results rather than only a deploy.

---

## Design rules

- **Nothing outside the closed set gets through.** Every model output is mapped
  onto a declared vocabulary, band, or number, and re-checked in Python *after*
  the block returns.
- **The deterministic half re-derives, it does not trust.** Values are re-parsed and re-guarded after the block
  returns, and a reading that fails its guard is stored as rejected rather
  than silently dropped.
- **Untrusted input is labelled as such.** The prompt is built in contract code;
  no caller string reaches the instruction part. Evidence sits inside tags and is
  named as data that is never an instruction, and text addressing the model is
  itself grounds for refusing to answer.
- **Refusing is a designed outcome.** A rejected reading, a missing field and an unreadable page all
  surface as `present: false`. A primitive that must always
  produce an answer will produce a wrong one.
- **Frozen at registration.** The url, the field names, the tolerances, and the guards. If these could be chosen later, whoever
  triggered the call would be choosing what the network reads.

## Further reading in this repository

- [CONTRACTS.md](CONTRACTS.md) — the full specification: purpose, consensus,
  state model, API, reuse
- [DECISIONS.md](DECISIONS.md) — engineering decisions, what testing found, and
  the honest limits
- [brand/](brand/) — the mark, the lockup, the palette, and the social card
- [lib/tolerance_consensus.py](lib/tolerance_consensus.py) — the agreement rules on
  their own, to be copied

## Related work

A separate primitive, built to the same standard and submitted independently:
[Crosscheck](https://github.com/meitipro/genlayer-crosscheck) — a framing-sensitivity detector for LLM-backed contracts.

The two share an author and a discipline, not a codebase. Each deploys, tests
and is used entirely on its own.

---

Published by [InferNode](https://x.com/Infer_node).
