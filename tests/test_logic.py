"""
Unit tests for the consensus logic, run with plain pytest. No GenVM needed.

WHY THIS FILE EXISTS
    In all three contracts the interesting part is not the prompt, it is the
    pure function that decides whether two validators agree. Those functions are
    deliberately module level and side-effect free so they can be tested here,
    exhaustively and in milliseconds, without Studio or a network.

HOW IT LOADS THE CONTRACTS
    A contract file cannot simply be imported: it starts with the GenVM
    dependency header and does `from genlayer import *`, which only resolves
    inside GenVM. So this file reads the real contract source and executes only
    the part above the storage section, with a small stub standing in for the
    genlayer module.

    That matters: these tests run against the exact code that ships. There is no
    second copy of the logic to drift out of sync.

    Run with:  pytest tests/test_logic.py -v
"""

import pathlib
import sys
import types

import pytest

CONTRACTS = pathlib.Path(__file__).resolve().parent.parent / "contracts"


def load_pure(filename):
    """Execute a contract's pure helper section with genlayer stubbed out."""
    src = pathlib.Path(CONTRACTS / filename).read_text(encoding="utf-8")

    # Everything above the storage section is pure Python by construction.
    marker = "# Storage"
    assert marker in src, f"{filename} is missing its storage section marker"
    head = src.split(marker)[0]

    # Drop the two lines that only resolve inside GenVM.
    head = "\n".join(
        line
        for line in head.splitlines()
        if not line.startswith("from genlayer import")
        and not line.startswith("# { \"Depends\"")
    )

    # Minimal stubs for the names the helper section touches.
    stub = types.ModuleType("genlayer_stub")

    def allow_storage(cls):
        return cls

    ns = {
        "allow_storage": allow_storage,
        "dataclass": (lambda c: c),
        "typing": __import__("typing"),
        "u256": int,
        "u8": int,
        "__name__": f"pure_{filename}",
    }
    exec(compile(head, filename, "exec"), ns)
    return types.SimpleNamespace(**ns)


tolerance = load_pure("tolerance.py")


class TestParseMode:
    def test_exact_takes_no_parameter(self):
        assert tolerance.parse_mode("exact") == ("exact", "")

    def test_abs_and_pct(self):
        assert tolerance.parse_mode("abs:0.5") == ("abs", "0.5")
        assert tolerance.parse_mode("pct:2.5") == ("pct", "2.5")

    def test_band_keeps_its_edges(self):
        assert tolerance.parse_mode("band:100,1000") == ("band", "100,1000")

    def test_unknown_mode_is_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_mode("roughly")

    def test_abs_without_a_parameter_is_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_mode("abs")

    def test_negative_tolerance_is_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_mode("pct:-1")

    def test_unsorted_bands_are_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_mode("band:1000,100")

    def test_duplicate_band_edges_are_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_mode("band:100,100")

    @pytest.mark.parametrize("spec", ["abs:inf", "pct:nan", "abs:1e400", "band:0,inf"])
    def test_non_finite_tolerances_are_refused(self, spec):
        """abs:inf is no agreement rule at all. pct:nan is a field that can
        never reach consensus, silently, forever."""
        with pytest.raises(ValueError):
            tolerance.parse_mode(spec)


class TestCoerceNumber:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (42, 42.0),
            (4.5, 4.5),
            ("1204", 1204.0),
            ("1,204", 1204.0),
            ("$12.50", 12.5),
            ("3.2%", 3.2),
            (" 7 ", 7.0),
        ],
    )
    def test_shapes_models_actually_emit(self, raw, expected):
        assert tolerance.coerce_number(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "n/a", "unknown", "about ten", "-", True])
    def test_anything_ambiguous_becomes_missing(self, raw):
        assert tolerance.coerce_number(raw) is tolerance.MISSING

    @pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "1e400", "-1e400", "9" * 400])
    def test_non_finite_numbers_become_missing(self, raw):
        """Infinity passes every <= range check ever written, and NaN compares
        false against itself. Neither is a number a page can print."""
        assert tolerance.coerce_number(raw) is tolerance.MISSING

    def test_a_number_that_underflows_is_still_a_number(self):
        assert tolerance.coerce_number("1e-400") == 0.0


class TestWithin:
    def test_exact_forgives_only_float_noise(self):
        assert tolerance.within("exact", "", 1.0, 1.0 + 1e-12) is True
        assert tolerance.within("exact", "", 1204, 1205) is False

    def test_abs_tolerance(self):
        assert tolerance.within("abs", "1", 1204, 1205) is True
        assert tolerance.within("abs", "1", 1204, 1206) is False

    def test_pct_tolerance_scales_with_the_number(self):
        assert tolerance.within("pct", "1", 1000, 1005) is True
        assert tolerance.within("pct", "1", 1000, 1020) is False
        # the same absolute gap on a small number fails
        assert tolerance.within("pct", "1", 10, 15) is False

    def test_pct_at_zero_does_not_divide_by_zero(self):
        assert tolerance.within("pct", "5", 0, 0) is True
        assert tolerance.within("pct", "5", 0, 1) is False

    def test_band_collapses_noise_before_comparing(self):
        # this is the point of band: 1204 and 1288 are far apart and agree
        assert tolerance.within("band", "100,1000", 1204, 1288) is True
        # and 999 vs 1001 do not, because they straddle a declared boundary
        assert tolerance.within("band", "100,1000", 999, 1001) is False

    def test_band_boundaries_are_lower_inclusive(self):
        assert tolerance.band_of(100, "100,1000") == 1
        assert tolerance.band_of(99.999, "100,1000") == 0
        assert tolerance.band_of(1000, "100,1000") == 2

    def test_missing_on_one_side_only_is_a_disagreement(self):
        assert tolerance.within("exact", "", None, 5) is False
        assert tolerance.within("exact", "", 5, None) is False

    def test_missing_on_both_sides_agrees(self):
        assert tolerance.within("exact", "", None, None) is True

    @pytest.mark.parametrize(
        "mode,param,a,b",
        [
            ("exact", "", 1.0, 1.0),
            ("abs", "1", 1204, 1205),
            ("pct", "1", 1000, 1005),
            ("band", "100,1000", 1204, 1288),
            ("abs", "1", 1204, 1210),
            ("band", "100,1000", 999, 1001),
        ],
    )
    def test_comparison_is_symmetric(self, mode, param, a, b):
        # an asymmetric rule would make consensus depend on who led
        assert tolerance.within(mode, param, a, b) == tolerance.within(mode, param, b, a)


class TestReadingsAgree:
    SPECS = [
        ("fee_pct", "exact", ""),
        ("visitors", "pct", "5"),
        ("balance", "band", "1000,100000"),
    ]

    def test_every_field_passing_its_own_rule(self):
        mine = {"fee_pct": 2.5, "visitors": 1000, "balance": 50000}
        theirs = {"fee_pct": 2.5, "visitors": 1040, "balance": 61234}
        assert tolerance.readings_agree(mine, theirs, self.SPECS) is True

    def test_a_volatile_field_does_not_loosen_a_strict_one(self):
        # visitors drifting is fine, fee moving is not
        mine = {"fee_pct": 2.5, "visitors": 1000, "balance": 50000}
        theirs = {"fee_pct": 2.6, "visitors": 1000, "balance": 50000}
        assert tolerance.readings_agree(mine, theirs, self.SPECS) is False

    def test_a_strict_field_does_not_tighten_a_volatile_one(self):
        mine = {"fee_pct": 2.5, "visitors": 1000, "balance": 50000}
        theirs = {"fee_pct": 2.5, "visitors": 1049, "balance": 50000}
        assert tolerance.readings_agree(mine, theirs, self.SPECS) is True

    def test_string_values_from_calldata_are_parsed(self):
        mine = {"fee_pct": 2.5, "visitors": 1000, "balance": 50000}
        theirs = {"fee_pct": "2.5", "visitors": "1010", "balance": "50000"}
        assert tolerance.readings_agree(mine, theirs, self.SPECS) is True

    def test_a_missing_field_on_one_side_breaks_agreement(self):
        mine = {"fee_pct": 2.5, "visitors": 1000, "balance": 50000}
        theirs = {"fee_pct": 2.5, "visitors": None, "balance": 50000}
        assert tolerance.readings_agree(mine, theirs, self.SPECS) is False

    def test_garbage_calldata_is_rejected(self):
        mine = {"fee_pct": 2.5, "visitors": 1000, "balance": 50000}
        assert tolerance.readings_agree(mine, None, self.SPECS) is False


class TestParseGuard:
    def test_empty_guard(self):
        assert tolerance.parse_guard("") == ("", "")

    def test_step_only(self):
        assert tolerance.parse_guard("step:400") == ("400", "")

    def test_range_only(self):
        assert tolerance.parse_guard("range:0,100") == ("", "0,100")

    def test_both_guards(self):
        assert tolerance.parse_guard("step:400;range:0,100") == ("400", "0,100")

    def test_order_does_not_matter(self):
        assert tolerance.parse_guard("range:0,100;step:400") == ("400", "0,100")

    def test_unknown_guard_is_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_guard("wobble:3")

    def test_step_without_a_number_is_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_guard("step:")

    def test_negative_step_is_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_guard("step:-5")

    def test_one_sided_range_is_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_guard("range:100")

    def test_inverted_range_is_refused(self):
        with pytest.raises(ValueError):
            tolerance.parse_guard("range:100,0")

    @pytest.mark.parametrize(
        "spec", ["step:inf", "step:nan", "range:0,inf", "range:nan,1", "step:1e400"]
    )
    def test_non_finite_guards_are_refused(self, spec):
        with pytest.raises(ValueError):
            tolerance.parse_guard(spec)


class TestFinite:
    @pytest.mark.parametrize("x", [0, 1, -1, 3.5, "42", "-0.001", 1e300])
    def test_ordinary_numbers_pass(self, x):
        assert tolerance.finite(x, "x") == float(x)

    @pytest.mark.parametrize("x", ["inf", "-inf", "nan", "1e400", float("inf")])
    def test_non_finite_is_refused(self, x):
        with pytest.raises(ValueError):
            tolerance.finite(x, "x")

    def test_the_message_names_the_field(self):
        with pytest.raises(ValueError, match="step"):
            tolerance.finite("inf", "step")


class TestGuard:
    def test_no_guard_allows_anything(self):
        assert tolerance.guard_ok("", "", 5, 5_000_000) == ""

    def test_a_plausible_move_passes(self):
        assert tolerance.guard_ok("100", "", 1000, 1050) == ""

    def test_the_ten_thousandfold_hallucination_is_stopped(self):
        # both validators agreed they read this number. it is still wrong.
        assert tolerance.guard_ok("100", "", 1000, 10_000_000) != ""

    def test_a_move_downward_is_bounded_too(self):
        assert tolerance.guard_ok("100", "", 1000, 500) != ""

    def test_a_first_reading_has_no_step_to_check(self):
        assert tolerance.guard_ok("100", "", None, 5000) == ""

    def test_but_a_range_still_catches_a_first_reading(self):
        # the hole a step bound cannot close: there is no previous point, so
        # only an absolute envelope can say this was never believable
        assert tolerance.guard_ok("100", "0,100", None, 5000) != ""

    def test_a_value_inside_the_range_passes(self):
        assert tolerance.guard_ok("", "0,100", None, 42) == ""

    def test_range_boundaries_are_inclusive(self):
        assert tolerance.guard_ok("", "0,100", None, 0) == ""
        assert tolerance.guard_ok("", "0,100", None, 100) == ""
        assert tolerance.guard_ok("", "0,100", None, 100.0001) != ""

    def test_a_missing_value_is_absent_not_implausible(self):
        assert tolerance.guard_ok("100", "0,100", 50, None) == ""

    def test_the_reason_names_which_guard_failed(self):
        assert "range" in tolerance.guard_ok("", "0,100", None, 500)
        assert "step" in tolerance.guard_ok("10", "", 100, 500)

    def test_range_is_checked_before_step(self):
        # a value that breaks both should report the more fundamental problem
        why = tolerance.guard_ok("10", "0,100", 95, 5000)
        assert "range" in why


# ===========================================================================
# Cross-cutting: prompts must not let caller text reach the instructions
# ===========================================================================


class TestPromptHardening:
    def test_the_page_is_labelled_untrusted(self):
        p = tolerance.build_prompt("PAGE BODY", [("fee", "exact", "")])
        assert "untrusted" in p and "<page>" in p
        assert "never an instruction" in p

    def test_the_prompt_forbids_guessing(self):
        p = tolerance.build_prompt("page", [("fee", "exact", "")])
        assert "Never guess" in p
        assert "return null" in p

    def test_each_field_carries_its_rule_into_the_prompt(self):
        p = tolerance.build_prompt("page", [("fee", "pct", "5")])
        assert "pct:5" in p
