"""
End-to-end tests. The real contract files, executed.

tests/test_logic.py covers the pure agreement rules. This file covers everything
they cannot reach: the deterministic half of each method, storage round-trips,
the re-derivation checks, the plausibility gate, and the branches that only fire
when the leader and a validator see different things.

It runs on tests/glsim.py, a small GenVM stand-in, so it needs no Studio and no
network:

    pytest tests/test_e2e.py -v

The important trick is set_mocks(): the leader and the validator get their own
web pages and their own prompt answers. Any contract that quietly assumes both
nodes see identical bytes fails here rather than on a real network.
"""

import pytest

import glsim as S


PAGE = (
    "Status page. The mainnet contracts are verified on the explorer. "
    "Withdrawal fee is 0.4 percent. Visitors today: 1,204. "
    "Treasury balance: 50,000 GEN."
) * 3

BLANK = "   "
DOWN = S.UserError("connection refused")


GOOD = {"fee_pct": 0.4, "visitors": 1204, "balance": 50000}

class TestTolerance:
    def deploy(self):
        c = S.deploy("contracts/tolerance.py")
        S.call(
            c, "define", "status page", "https://a.example/s",
            ["fee_pct", "visitors", "balance"],
            ["exact", "pct:5", "band:1000,100000"],
            ["range:0,100", "", "step:40000;range:0,100000000"],
        )
        return c

    def mocks(self, values, v_values=None, page=PAGE, v_page=None):
        S.set_mocks(
            leader_pages={"a.example": page},
            leader_prompts={"You are extracting numbers": values},
            validator_pages={"a.example": v_page if v_page is not None else page},
            validator_prompts={
                "You are extracting numbers": v_values if v_values is not None else values
            },
        )

    # -- the happy path ----------------------------------------------------

    def test_a_clean_reading(self):
        c = self.deploy()
        self.mocks(GOOD)
        S.call(c, "read", 0)
        out = c.latest(0)
        assert out["accepted"] is True
        assert out["values"] == {"fee_pct": "0.4", "visitors": "1204.0",
                                 "balance": "50000.0"}
        assert c.value(0, "fee_pct") == {
            "present": True, "number": "0.4", "scaled": 400_000, "scale": 1_000_000,
        }

    def test_messy_number_shapes_are_parsed(self):
        c = self.deploy()
        self.mocks({"fee_pct": "0.4%", "visitors": "1,204", "balance": "$50,000"})
        S.call(c, "read", 0)
        assert c.latest(0)["values"]["balance"] == "50000.0"

    def test_a_missing_field_is_null_not_a_guess(self):
        c = self.deploy()
        self.mocks({"fee_pct": 0.4, "visitors": "n/a", "balance": 50000})
        S.call(c, "read", 0)
        assert c.latest(0)["values"]["visitors"] == ""
        assert c.value(0, "visitors")["present"] is False
        assert c.latest(0)["accepted"] is True      # absent is not implausible

    # -- per-field tolerance, the whole point ------------------------------

    def test_a_volatile_field_may_drift_within_its_own_rule(self):
        c = self.deploy()
        self.mocks(GOOD, v_values={"fee_pct": 0.4, "visitors": 1240, "balance": 50000})
        S.call(c, "read", 0)
        assert c.latest(0)["accepted"] is True

    def test_a_strict_field_may_not_drift_at_all(self):
        c = self.deploy()
        self.mocks(GOOD, v_values={"fee_pct": 0.5, "visitors": 1204, "balance": 50000})
        with pytest.raises(S.UserError):
            S.call(c, "read", 0)

    def test_a_banded_field_tolerates_a_large_move_inside_its_bucket(self):
        c = self.deploy()
        self.mocks(GOOD, v_values={"fee_pct": 0.4, "visitors": 1204, "balance": 61234})
        S.call(c, "read", 0)
        assert c.latest(0)["accepted"] is True

    def test_a_banded_field_does_not_tolerate_crossing_a_boundary(self):
        c = self.deploy()
        self.mocks(
            {"fee_pct": 0.4, "visitors": 1204, "balance": 99999},
            v_values={"fee_pct": 0.4, "visitors": 1204, "balance": 100001},
        )
        with pytest.raises(S.UserError):
            S.call(c, "read", 0)

    def test_a_fabricated_extra_field_is_rejected(self):
        """It could not reach storage anyway, since the deterministic half only
        walks the frozen field list, but a malformed proposal is cheap to
        refuse and refusing it keeps the failure legible."""
        c = self.deploy()
        self.mocks(GOOD)
        real = S._run_nondet_unsafe

        def lying(leader_fn, validator_fn):
            S.RT.active = S.RT.leader_env
            out = leader_fn()
            out["values"]["ghost"] = "1.0"
            S.RT.active = S.RT.validator_env
            ok = bool(validator_fn(S.Return(out)))
            S.RT.active = None
            if not ok:
                raise S.UserError("validators did not agree with the leader")
            return out

        S.gl.vm.run_nondet_unsafe = staticmethod(lying)
        try:
            with pytest.raises(S.UserError):
                S.call(c, "read", 0)
        finally:
            S.gl.vm.run_nondet_unsafe = staticmethod(real)

    def test_a_field_found_by_one_node_only_is_a_disagreement(self):
        c = self.deploy()
        self.mocks(GOOD, v_values={"fee_pct": 0.4, "visitors": None, "balance": 50000})
        with pytest.raises(S.UserError):
            S.call(c, "read", 0)

    # -- the plausibility gate ---------------------------------------------

    def test_an_absurd_jump_is_recorded_but_not_accepted(self):
        c = self.deploy()
        self.mocks(GOOD)
        S.call(c, "read", 0)
        self.mocks({"fee_pct": 0.4, "visitors": 1204, "balance": 90_000_000})
        S.call(c, "read", 0)
        out = c.latest(0)
        assert out["accepted"] is False
        assert "step" in out["rejected_because"]
        assert c.value(0, "balance")["present"] is False

    def test_a_second_absurd_reading_is_still_rejected(self):
        """The baseline is the last ACCEPTED reading, not the last reading.

        Taking the last reading would compare an absurd value against nothing
        and let the next one straight through.
        """
        c = self.deploy()
        self.mocks(GOOD)
        S.call(c, "read", 0)
        for balance in (90_000_000, 80_000_000, 70_000_000):
            self.mocks({"fee_pct": 0.4, "visitors": 1204, "balance": balance})
            S.call(c, "read", 0)
            assert c.latest(0)["accepted"] is False

    def test_a_plausible_reading_recovers_after_rejections(self):
        c = self.deploy()
        self.mocks(GOOD)
        S.call(c, "read", 0)
        self.mocks({"fee_pct": 0.4, "visitors": 1204, "balance": 90_000_000})
        S.call(c, "read", 0)
        self.mocks({"fee_pct": 0.4, "visitors": 1204, "balance": 52_000})
        S.call(c, "read", 0)
        assert c.latest(0)["accepted"] is True
        assert c.value(0, "balance")["number"] == "52000.0"

    def test_a_range_guard_catches_the_very_first_reading(self):
        """A step bound can say nothing about a first reading. Only an absolute
        envelope can, which is why fields carry both."""
        c = self.deploy()
        self.mocks({"fee_pct": 250.0, "visitors": 1204, "balance": 50000})
        S.call(c, "read", 0)
        out = c.latest(0)
        assert out["accepted"] is False
        assert "range" in out["rejected_because"]

    def test_an_unbounded_field_is_never_gated(self):
        c = self.deploy()
        self.mocks(GOOD)
        S.call(c, "read", 0)
        self.mocks({"fee_pct": 0.4, "visitors": 9_000_000, "balance": 50000},
                   v_values={"fee_pct": 0.4, "visitors": 9_000_000, "balance": 50000})
        S.call(c, "read", 0)
        assert c.latest(0)["accepted"] is True   # visitors declared no guard

    # -- unreadable pages --------------------------------------------------

    def test_a_blank_page_is_a_rejected_reading_not_a_failed_transaction(self):
        c = self.deploy()
        self.mocks(GOOD, page=BLANK)
        S.call(c, "read", 0)
        out = c.latest(0)
        assert out["accepted"] is False
        assert out["rejected_because"] == "the page could not be read"
        assert all(v == "" for v in out["values"].values())

    def test_an_unreachable_page_is_the_same(self):
        c = self.deploy()
        self.mocks(GOOD, page=DOWN)
        S.call(c, "read", 0)
        assert c.latest(0)["rejected_because"] == "the page could not be read"

    def test_an_unreadable_page_does_not_become_the_baseline(self):
        c = self.deploy()
        self.mocks(GOOD)
        S.call(c, "read", 0)
        self.mocks(GOOD, page=DOWN)
        S.call(c, "read", 0)
        self.mocks({"fee_pct": 0.4, "visitors": 1204, "balance": 90_000_000})
        S.call(c, "read", 0)
        assert c.latest(0)["accepted"] is False   # still measured against 50000

    def test_nodes_disagreeing_about_readability_do_not_agree(self):
        c = self.deploy()
        self.mocks(GOOD, page=PAGE, v_page=BLANK)
        with pytest.raises(S.UserError):
            S.call(c, "read", 0)

    def test_both_nodes_finding_it_unreadable_agree(self):
        c = self.deploy()
        self.mocks(GOOD, page=BLANK, v_page=BLANK)
        S.call(c, "read", 0)
        assert c.meter(0)["readings"] == 1

    # -- hostile numbers ---------------------------------------------------

    def test_infinity_and_nan_from_the_model_are_read_as_absent(self):
        """A model returning "inf" would otherwise pass every range check, and
        "nan" would make the field silently un-agreeable forever."""
        c = self.deploy()
        self.mocks({"fee_pct": "inf", "visitors": "nan", "balance": "1e400"})
        S.call(c, "read", 0)
        out = c.latest(0)
        assert all(v == "" for v in out["values"].values())
        assert c.value(0, "fee_pct")["present"] is False

    def test_a_non_finite_tolerance_cannot_be_defined(self):
        c = S.deploy("contracts/tolerance.py")
        for tol in ("abs:inf", "pct:nan", "band:0,inf"):
            with pytest.raises(S.UserError):
                S.call(c, "define", "m", "https://a.example/s", ["x"], [tol], [""])
        assert c.count() == 0

    def test_a_non_finite_guard_cannot_be_defined(self):
        c = S.deploy("contracts/tolerance.py")
        for g in ("step:inf", "range:0,inf", "step:1e400"):
            with pytest.raises(S.UserError):
                S.call(c, "define", "m", "https://a.example/s", ["x"], ["exact"], [g])
        assert c.count() == 0

    # -- views -------------------------------------------------------------

    def test_value_is_safe_before_any_reading(self):
        c = self.deploy()
        assert c.latest(0)["read"] is False
        empty = {"present": False, "number": "", "scaled": 0, "scale": 1_000_000}
        assert c.value(0, "fee_pct") == empty
        assert c.value(0, "no_such_field") == empty

    def test_no_float_crosses_the_calldata_boundary(self):
        """A consuming contract cannot do reliable float arithmetic on chain,
        and calldata is not the place to find out whether floats encode."""
        c = self.deploy()
        self.mocks(GOOD)
        S.call(c, "read", 0)
        for v in c.latest(0)["values"].values():
            assert isinstance(v, str)
        got = c.value(0, "visitors")
        assert isinstance(got["number"], str)
        assert isinstance(got["scaled"], int)
        assert got["scaled"] == 1_204_000_000
        assert got["scaled"] // got["scale"] == 1204

    def test_the_meter_publishes_both_rules(self):
        c = self.deploy()
        f = {x["name"]: x for x in c.meter(0)["fields"]}
        assert f["fee_pct"]["tolerance"] == "exact"
        assert f["fee_pct"]["range"] == "0,100"
        assert f["balance"]["tolerance"] == "band:1000,100000"
        assert f["balance"]["step"] == "40000"
        assert f["visitors"]["step"] == "" and f["visitors"]["range"] == ""

    # -- validation --------------------------------------------------------

    @pytest.mark.parametrize(
        "names,tols,guards",
        [
            (["x"], ["roughly"], [""]),                  # unknown mode
            (["x"], ["abs"], [""]),                      # abs without a parameter
            (["x"], ["pct:-1"], [""]),                   # negative tolerance
            (["x"], ["band:1000,100"], [""]),            # unsorted band
            (["x"], ["band:100,100"], [""]),             # duplicate edges
            (["x", "x"], ["exact", "exact"], ["", ""]),  # duplicate names
            (["", ], ["exact"], [""]),                   # empty name
            (["a", "b"], ["exact"], [""]),               # length mismatch
            (["x"], ["exact"], ["wobble:3"]),            # unknown guard
            (["x"], ["exact"], ["step:"]),               # step without a number
            (["x"], ["exact"], ["step:-5"]),             # negative step
            (["x"], ["exact"], ["range:100"]),           # one sided range
            (["x"], ["exact"], ["range:100,0"]),         # inverted range
        ],
    )
    def test_bad_definitions_are_refused(self, names, tols, guards):
        c = S.deploy("contracts/tolerance.py")
        with pytest.raises(S.UserError):
            S.call(c, "define", "m", "https://a.example/s", names, tols, guards)
        assert c.count() == 0

    def test_a_non_http_url_is_refused(self):
        c = S.deploy("contracts/tolerance.py")
        with pytest.raises(S.UserError):
            S.call(c, "define", "m", "ftp://a.example/s", ["x"], ["exact"], [""])

    def test_too_many_fields_is_refused(self):
        c = S.deploy("contracts/tolerance.py")
        n = [f"f{i}" for i in range(20)]
        with pytest.raises(S.UserError):
            S.call(c, "define", "m", "https://a.example/s", n,
                   ["exact"] * 20, [""] * 20)

    def test_out_of_range_ids_are_refused(self):
        c = S.deploy("contracts/tolerance.py")
        for bad in (0, 5, -1):
            with pytest.raises(S.UserError):
                S.call(c, "read", bad)


# ===========================================================================
# Cross-cutting: nothing is written when a transaction fails
# ===========================================================================


class TestAtomicity:
    def test_nothing_is_written_when_a_definition_fails(self):
        c = S.deploy("contracts/tolerance.py")
        with pytest.raises(S.UserError):
            S.call(c, "define", "m", "https://a.e/s",
                   ["a", "b"], ["exact", "nonsense"], ["", ""])
        assert c.count() == 0
