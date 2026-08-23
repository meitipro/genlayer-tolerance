"""
Integration tests, run against GenLayer Studio with gltest.

    pip install genlayer-test
    gltest --network studionet tests/test_integration.py

These are slower than tests/test_logic.py and they prove something different:
that the contracts deploy, that storage round-trips, that the deterministic
gates fire, and that the whole leader-plus-validator cycle completes.

The web page and the model are both mocked, so a run is deterministic and needs
no network. Mocks match by substring against the message the runtime builds, so
the keys below are fragments of the prompts in contracts/.
"""

import pytest

# gltest is only needed for this file. Skip cleanly when it is absent so that
# `pytest tests/` works out of the box on a machine with nothing installed but
# pytest, and still runs everything in test_logic.py and test_e2e.py.
gltest = pytest.importorskip(
    "gltest",
    reason="integration tests need genlayer-test and a running Studio: "
           "pip install genlayer-test, then gltest --network studionet",
)
from gltest import get_contract_factory                      # noqa: E402
from gltest.assertions import (                              # noqa: E402
    tx_execution_succeeded,
    tx_execution_failed,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


LIVE_PAGE = (
    "Status page. The mainnet contracts are verified on the explorer. "
    "Withdrawal fee is 0.4 percent. Visitors today: 1,204. "
    "Treasury balance: 50,000 GEN. Last updated one minute ago."
) * 3

EMPTY_PAGE = "  "


def web(mapping):
    """Build a mocked web response table keyed by url."""
    return {"nondet_web_render": mapping}


def llm(mapping):
    """Build a mocked prompt response table keyed by prompt substring."""
    return {"nondet_exec_prompt": mapping}


def merge(*ds):
    out = {}
    for d in ds:
        out.update(d)
    return out

class TestTolerance:
    @pytest.fixture
    def contract(self):
        factory = get_contract_factory("Contract", contract_file="contracts/tolerance.py")
        return factory.deploy(args=[])

    def _define(self, contract):
        tx = contract.define(
            args=[
                "example status page",
                "https://a.example/status",
                "fee_pct|visitors|balance",
                "exact|pct:5|band:1000,100000",
                "range:0,100||step:40000;range:0,100000000",
            ]
        )
        assert tx_execution_succeeded(tx)

    def test_define_then_read(self, contract):
        self._define(contract)
        mocks = merge(
            web({"a.example": LIVE_PAGE}),
            llm({"You are extracting numbers": {
                "fee_pct": 0.4, "visitors": 1204, "balance": 50000
            }}),
        )
        tx = contract.read(args=[0], mock_response=mocks)
        assert tx_execution_succeeded(tx)

        out = contract.latest(args=[0]).call()
        assert out["read"] is True
        assert out["accepted"] is True
        assert out["values"]["fee_pct"] == "0.4"
        assert out["values"]["visitors"] == "1204.0"

    def test_messy_number_shapes_are_parsed(self, contract):
        self._define(contract)
        mocks = merge(
            web({"a.example": LIVE_PAGE}),
            llm({"You are extracting numbers": {
                "fee_pct": "0.4%", "visitors": "1,204", "balance": "$50,000"
            }}),
        )
        tx = contract.read(args=[0], mock_response=mocks)
        assert tx_execution_succeeded(tx)
        out = contract.latest(args=[0]).call()
        assert out["values"]["visitors"] == "1204.0"
        assert out["values"]["balance"] == "50000.0"

    def test_a_missing_field_is_null_not_a_guess(self, contract):
        self._define(contract)
        mocks = merge(
            web({"a.example": LIVE_PAGE}),
            llm({"You are extracting numbers": {
                "fee_pct": 0.4, "visitors": None, "balance": 50000
            }}),
        )
        tx = contract.read(args=[0], mock_response=mocks)
        assert tx_execution_succeeded(tx)
        assert contract.latest(args=[0]).call()["values"]["visitors"] == ""
        assert contract.value(args=[0, "visitors"]).call()["present"] is False

    def test_the_guard_rejects_an_implausible_jump(self, contract):
        """Both validators agreed they read the same absurd number.

        Agreement is not truth. The guard is deterministic, runs after
        consensus, and refuses to store a value that moved further than the
        field declared it ever could.
        """
        self._define(contract)
        first = merge(
            web({"a.example": LIVE_PAGE}),
            llm({"You are extracting numbers": {
                "fee_pct": 0.4, "visitors": 1204, "balance": 50000
            }}),
        )
        assert tx_execution_succeeded(contract.read(args=[0], mock_response=first))

        absurd = merge(
            web({"a.example": LIVE_PAGE}),
            llm({"You are extracting numbers": {
                "fee_pct": 0.4, "visitors": 1204, "balance": 500_000_000
            }}),
        )
        tx = contract.read(args=[0], mock_response=absurd)
        assert tx_execution_succeeded(tx)          # the reading is recorded

        out = contract.latest(args=[0]).call()
        assert out["accepted"] is False             # but it is not accepted
        assert "step" in out["rejected_because"]
        # and a consuming contract sees nothing rather than the absurd value
        assert contract.value(args=[0, "balance"]).call()["present"] is False

    # -- input validation --------------------------------------------------

    def test_unknown_tolerance_mode_is_refused(self, contract):
        tx = contract.define(
            args=["m", "https://a.example/s", ["x"], ["roughly"], [""]]
        )
        assert tx_execution_failed(tx)

    def test_unknown_guard_is_refused(self, contract):
        tx = contract.define(
            args=["m", "https://a.example/s", ["x"], ["exact"], ["wobble:3"]]
        )
        assert tx_execution_failed(tx)

    def test_inverted_range_guard_is_refused(self, contract):
        tx = contract.define(
            args=["m", "https://a.example/s", ["x"], ["exact"], ["range:100,0"]]
        )
        assert tx_execution_failed(tx)

    def test_abs_without_a_parameter_is_refused(self, contract):
        tx = contract.define(args=["m", "https://a.example/s", ["x"], ["abs"], [""]])
        assert tx_execution_failed(tx)

    def test_unsorted_band_edges_are_refused(self, contract):
        tx = contract.define(
            args=["m", "https://a.example/s", ["x"], ["band:1000,100"], [""]]
        )
        assert tx_execution_failed(tx)

    def test_mismatched_list_lengths_are_refused(self, contract):
        tx = contract.define(
            args=["m", "https://a.example/s", ["a", "b"], ["exact"], [""]]
        )
        assert tx_execution_failed(tx)

    def test_duplicate_field_names_are_refused(self, contract):
        tx = contract.define(
            args=["m", "https://a.example/s", ["x", "x"], ["exact", "exact"], ["", ""]]
        )
        assert tx_execution_failed(tx)
