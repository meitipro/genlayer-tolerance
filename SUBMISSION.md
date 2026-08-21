# Submission

One submission, under **Builder -> Intelligent Contracts**. This repository is
one standalone primitive. It is not part of a larger project and does not depend
on anything else.

---

## Before you submit, in order

1. **Deploy and exercise.** `./scripts/deploy.sh studionet`. It deploys the
   contract and then calls its methods, so the explorer page shows real
   transactions with consensus results rather than only a deploy.

2. **Open the explorer page and check it.** It must show a Deploy transaction
   **and** at least one method call with a Consensus Result beside it. A page
   with only a deploy proves the file compiles and nothing else. This is the
   strongest single artifact in the submission.

3. **Paste the address** into README.md and into this file wherever the
   placeholder appears, then push.

4. **Submit** with the title, notes and links below.

---

## Title

```
Tolerance: per-field numeric agreement and plausibility guards
```

## Notes (985 characters, the box caps at 1000)

```
Tolerance is a reusable numeric extraction primitive where every field declares ON CHAIN how closely validators must agree about it. It fixes the commonest failure in Intelligent Contracts: a number wrapped in strict_eq works in Studio with one validator and never settles live, because two nodes fetch seconds apart and see 1,204 and 1,205. The contract uses gl.vm.run_nondet_unsafe: the leader extracts every field in one prompt, and the validator compares FIELD BY FIELD, each under its own frozen rule, exact, abs:X, pct:X or band:a,b,c. A volatile field never loosens a stable one. A second deterministic guard runs AFTER consensus: step bounds movement from the last ACCEPTED reading, range bounds where a value may ever be. A number both validators agreed on that jumped ten thousandfold is stored not accepted, because agreement is not truth. Reusable for price feeds, quota monitors, and treasury reporting. Deployed at {address} on studionet.
```

## Links

```
GitHub:   https://github.com/meitipro/genlayer-tolerance
Contract: https://github.com/meitipro/genlayer-tolerance/blob/main/contracts/tolerance.py
Explorer: https://explorer-studio.genlayer.com/address/{address}
```

---

## What clears the bar, line by line

The category rejects "thin LLM wrappers" and "generic AI decides X demos".

- **The model never decides.** It extracts numbers. Which numbers are acceptable, how far apart two
  nodes may be, and how far a value may move are all declared on chain before
  it runs.
- **The validator function is the contribution.** This contract exists to
  demonstrate one agreement rule, explained in [CONTRACTS.md](CONTRACTS.md)
  with the code beside it.
- **Refusing is designed.** present:false covers a missing field, a rejected reading, an unreadable
  page, and a meter never read.
- **The tests have teeth.** 137 passing is a claim; the mutation table in
  [README.md](README.md#the-tests-have-teeth) is evidence.
- **It runs with nothing installed.** `pip install pytest && pytest tests/ -q`.
  A reviewer with two minutes can verify the whole thing.
- **The limits are stated.** [DECISIONS.md](DECISIONS.md) says what this cannot
  do, including the case it structurally cannot detect.
