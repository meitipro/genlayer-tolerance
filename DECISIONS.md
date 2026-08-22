# DECISIONS

What was decided, what was found by running the code, and what is still true
that a reviewer should know.

---

## The four bugs, and why they matter more than the fixes

All four were found by **executing** the contract, not by reading it. Three are
invisible to a test that gives the leader and the validator the same mock data,
which is what almost every test suite does.

### 1 · A rejected reading became the baseline

The guard compared each reading against the previous one. When a reading was
rejected, the next absurd value had nothing to be measured against and was
accepted.

The attack is trivial: get one implausible value through, and the one after it
is unguarded.

**Fix:** walk back to the last **accepted** reading. One loop.
**Test:** `test_a_second_absurd_reading_is_still_rejected`.

### 2 · An unreadable page failed the transaction with the wrong error

Raising inside the non-deterministic block made the leader's result a
non-`Return`, so the validator voted no and the transaction died with
"validators did not agree" — exactly backwards, because both nodes had agreed
perfectly that the page could not be read.

Beyond the misleading error, a monitor whose source went down looked like a
consensus failure rather than a source outage, which is the difference between
paging an engineer and doing nothing.

**Fix:** unreadability is a value the block returns, both nodes must agree on it,
and it is stored as a rejected reading.
**Test:** `test_a_blank_page_is_a_rejected_reading_not_a_failed_transaction`.

### 3 · A step guard cannot protect a first reading

With no previous value there is nothing to compare against, so the very first
number was always accepted no matter how absurd. A step bound is structurally
incapable of catching it.

**Fix:** fields carry an absolute `range` alongside `step`. Two guards, because
they catch different lies.
**Test:** `test_a_range_guard_catches_the_very_first_reading`.

### 4 · Infinity and NaN reached the comparison logic

`float("1e400")` is infinity, and infinity passes every `<=` range check ever
written. NaN compares false against itself, so a `pct:nan` tolerance would
create a field that can never reach consensus — silently, forever, with no error
anywhere.

Both arrive easily: a model returning `"inf"`, a page printing a 400-digit
number, a caller typo in a tolerance.

**Fix:** `finite()` refuses both at every numeric entry point — model output,
tolerance parameters, guard parameters.
**Tests:** `test_non_finite_numbers_become_missing`,
`test_non_finite_tolerances_are_refused`, `test_non_finite_guards_are_refused`.


### A read with a negative id returned the newest record

Every view indexed `self.meters[int(meter_id)]` directly. Two failures came out
of one missing line.

An id past the end raised a raw `IndexError`, which GenVM reports as a
**contract error** rather than a user error — a caller learns nothing about
what went wrong.

The worse half: Python list indexing accepts `-1`. A caller asking for claim
`-1` silently received the **newest** meter's reading, correctly formatted,
with nothing failing anywhere. A consuming contract could act on it and never
know it had read a different meter.

**Fix:** one bounds-checked lookup helper, used by every read.
**Tests:** `test_a_read_with_a_nonexistent_id_is_a_user_error`,
`test_a_read_with_a_negative_id_does_not_return_the_last_record`.

---

## Why no float crosses the calldata boundary

`value()` and `latest()` originally returned Python floats. They now return the
canonical decimal string plus a fixed point integer at scale 1,000,000.

Two reasons, and the second is the real one:

1. GenVM calldata is a custom binary format, and a numeric primitive that cannot
   hand back its own number would be useless. Depending on float encoding is a
   bet with no upside.
2. **A consuming contract cannot do reliable float arithmetic on chain anyway.**
   Handing it a float invites it to compare, add and threshold in a way that
   will eventually disagree between nodes. A fixed point integer is what it can
   actually use, and the exact string is what it should display.

Covered by `test_no_float_crosses_the_calldata_boundary`.

---

## Why tolerance and plausibility are two separate rules

They answer different questions and conflating them is wrong in both
directions.

**Tolerance** asks: how far apart may two honest validators be, having fetched
the same page seconds apart? That is a property of the field's volatility.

**Plausibility** asks: was this number ever believable? That is a property of
the world.

A contract with only tolerance accepts an absurd value both nodes happened to
read. A contract with only plausibility never reaches consensus on a live
counter. The two are enforced in different places — tolerance inside the
validator, plausibility deterministically after consensus — because one is
about agreement and the other is about truth, and **agreement is not truth.**

---

## Why the tests are built the way they are

### The simulator gives each node its own world

[`tests/glsim.py`](tests/glsim.py) runs the block twice — once as the leader,
once as a validator — with **independent** mock pages and prompt answers:

```python
self.mocks(GOOD, v_values={"fee_pct": 0.5, ...})   # the validator read 0.5
```

Bugs 1, 2 and 3 are invisible to a suite that feeds both nodes the same data.
That is the default in every mocking framework, and it is why they survived
review.

### The unit tests load the real contract source

A contract file cannot simply be imported: it starts with the GenVM dependency
header and does `from genlayer import *`. So `tests/test_logic.py` reads the
real file and executes the helper section with a stub.

The alternative — copying `within()` into the test file — creates a second copy
that drifts. Here, a change to the contract is a change to what the tests run.

### Mutation testing, because passing tests prove nothing

Every safety property was broken on purpose to confirm a test notices. The table
is in [README.md](README.md#the-tests-have-teeth).

Two mutations initially escaped, and both were informative: a defence added
later was strict enough to catch cases an earlier test was supposed to cover,
which left those earlier tests unable to fail. Both were replaced with tests
that isolate one rule at a time. **A test that cannot fail is worse than no
test**, because it reports coverage it does not provide.

## The storage layout, and why it looks like this

Every collection in this contract is a **top level contract field**. No storage
dataclass contains a `DynArray`, and records carry an id rather than living
inside their parent.

That is not a style preference. It cost two failed deployments.

### What was tried, and what each attempt did

```python
@allow_storage
@dataclass
class Claim:
    checks: DynArray[Check]
```

**Attempt 1** — build it the obvious way:

```python
Claim(..., checks=DynArray[Check]())
# TypeError: this class can't be instantiated by user
```

**Attempt 2** — use the documented escape hatch. The storage page shows
`User(gl.storage.inmem_allocate(TreeMap[str, str]))` working for a nested
`TreeMap`, so the same shape should work for a nested `DynArray`:

```python
Claim(..., checks=gl.storage.inmem_allocate(DynArray[Check]))
# TypeError: _GenericAlias.__init__() missing 1 required positional argument: 'args'
```

It did not. The subscripted generic's `__init__` is not the collection's, so
the allocator calls the wrong one.

**Being precise about this:** the documentation does not say nested collections
are impossible, and `inmem_allocate` is documented as the way to build them. It
failed here for `DynArray[T]` on the deployed runner. Whether that is a version
difference, a difference between `TreeMap` and `DynArray`, or something about
the element type, is not something that could be settled from outside — and a
primitive should not depend on a mechanism that failed once and cannot be tested
locally.

### What the flat shape buys

Top level fields are allocated by the runtime, so nothing has to be constructed
in memory at all. `self.checks` simply exists, zero-initialised to `[]`, and a
`Check` carries the `claim_id` it belongs to.

The cost is a linear walk in the views instead of a direct index. Views are
free to the caller and never run inside a write, so that trade is a bargain for
a shape that cannot fail at deploy time.

### The other rules from the same page, all enforced here

- `list`, `dict` and `int` are **not valid storage types**. Use `DynArray[T]`,
  `TreeMap[K, V]`, and `u256` / `i256` / `bigint`.
- Only **fully instantiated** generics. Bare `TreeMap` is refused.
- Persistent fields must be **declared in the class body** with a type
  annotation. `self.something = value` on an undeclared name is not persistent
  and is silently discarded after execution.
- Storage objects **cannot be used inside a non-deterministic block**. Everything
  the block here closes over is extracted to a plain `str` or list first.
- Calldata mappings support **`str` keys only**, like JSON.
- A storage object is a **view on a slot, not a copy**. Holding a reference
  across a write to that slot gives you the new value, silently. Nothing here
  holds a reference across an append to the same array.

The test suite checks all of this by static analysis, and
[`tests/glsim.py`](tests/glsim.py) refuses at class definition time everything
GenVM refuses at deploy time — so a regression fails on the workstation in
0.2 seconds rather than after a deployment.

---
## A duplicated method shadowed the real one

While flattening the storage, an editing mistake left **two definitions of the
same lookup helper** in the contract. Python allows this silently: the second
definition wins and the first is dead code.

It surfaced through mutation testing. A mutation to the first copy changed
nothing, because the second copy was the one being called, and the mutation was
reported as escaping the tests. The tests were fine; the contract had a hidden
duplicate.

Two static checks now guard it — no method defined twice in a class, no
top-level name defined twice in the module. Both are one assertion each and
both would have caught it immediately.

---

## Honest limits

Things a reviewer should know that the README does not lead with.

### It trusts the page

If a page prints a wrong number, both validators read the wrong number, they
agree, and the guard passes it as long as it is plausible. The guard bounds
*implausibility*, not *falsehood*. Corroborating across independent sources is a
different primitive and is not attempted here.

### A band boundary is a cliff

Two values either side of a declared boundary never agree, however close. That
is the point, but it means boundaries should be placed where values are unlikely
to sit — not at round numbers a real figure might hover around. `band:1000` is a
poor choice for a counter that spends its time near 1,000.

### The guard needs a history to be useful

`step` does nothing on the first reading, which is why `range` exists. But
`range` requires knowing the plausible envelope in advance. A field where you
genuinely cannot state either bound gets no plausibility protection at all, and
the contract will not pretend otherwise: leaving the guard empty is allowed and
means exactly what it says.

### Extraction is one prompt

All fields are extracted together. That is cheap and keeps the fields
consistent with each other, and it means one badly worded field can degrade the
extraction of its neighbours. Splitting a meter in two is the remedy.

### Not upgradable

There is no admin method, no pause, no owner. That is deliberate for a primitive
whose value is that its rules cannot move after somebody depends on them, and it
means a bug found later requires a new deployment.
