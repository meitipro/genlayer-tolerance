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

CONTRACT_PATH = "contracts/tolerance.py"


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
            "fee_pct|visitors|balance",
            "exact|pct:5|band:1000,100000",
            "range:0,100||step:40000;range:0,100000000",
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
                S.call(c, "define", "m", "https://a.example/s", "x", tol, "")
        assert c.count() == 0

    def test_a_non_finite_guard_cannot_be_defined(self):
        c = S.deploy("contracts/tolerance.py")
        for g in ("step:inf", "range:0,inf", "step:1e400"):
            with pytest.raises(S.UserError):
                S.call(c, "define", "m", "https://a.example/s", "x", "exact", g)
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


    def test_a_read_with_a_nonexistent_id_is_a_user_error(self):
        """Not a raw IndexError. GenVM reports an uncaught Python exception as
        a contract error, which tells a caller nothing about what went wrong."""
        c = self.deploy()
        for method in ("meter", "latest"):
            with pytest.raises(S.UserError, match="no such meter"):
                getattr(c, method)(99)
        with pytest.raises(S.UserError, match="no such meter"):
            c.value(99, "fee_pct")

    def test_a_read_with_a_negative_id_does_not_return_the_last_record(self):
        """The dangerous half. Python list indexing accepts -1 and returns the
        newest meter, so a caller asking for meter -1 would silently receive a
        different meter's reading and never know."""
        c = self.deploy()
        self.mocks(GOOD)
        S.call(c, "read", 0)
        assert c.latest(0)["accepted"] is True
        for method in ("meter", "latest"):
            with pytest.raises(S.UserError, match="no such meter"):
                getattr(c, method)(-1)
        with pytest.raises(S.UserError, match="no such meter"):
            c.value(-1, "fee_pct")


    # -- isolation between meters ------------------------------------------

    def test_two_meters_do_not_read_each_other_s_fields_or_readings(self):
        """Fields, readings and values all live in flat arrays keyed by id.
        Nothing else keeps two meters apart, so this is the test that the ids
        are honoured.

        Without it, a lookup that ignored meter_id would still pass every other
        test in this file, because every other test uses a single meter.
        """
        c = S.deploy("contracts/tolerance.py")
        S.call(c, "define", "meter A", "https://a.example/s",
               "fee_pct", "exact", "range:0,100")
        S.call(c, "define", "meter B", "https://a.example/s",
               "visitors|balance", "pct:5|band:1000,100000", "|")

        a = c.meter(0)
        b = c.meter(1)
        assert a["label"] == "meter A"
        assert [f["name"] for f in a["fields"]] == ["fee_pct"]
        assert b["label"] == "meter B"
        assert [f["name"] for f in b["fields"]] == ["visitors", "balance"]

        self.mocks({"fee_pct": 0.4})
        S.call(c, "read", 0)
        self.mocks({"visitors": 1204, "balance": 50000})
        S.call(c, "read", 1)

        assert set(c.latest(0)["values"]) == {"fee_pct"}
        assert set(c.latest(1)["values"]) == {"visitors", "balance"}
        assert c.value(0, "fee_pct")["number"] == "0.4"
        assert c.value(0, "visitors")["present"] is False     # belongs to meter 1
        assert c.value(1, "visitors")["number"] == "1204.0"
        assert c.meter(0)["readings"] == 1
        assert c.meter(1)["readings"] == 1

    def test_one_meter_s_rejected_reading_does_not_move_another_s_baseline(self):
        c = S.deploy("contracts/tolerance.py")
        for label in ("A", "B"):
            S.call(c, "define", f"meter {label}", "https://a.example/s",
                   "v", "exact", "step:10;range:0,100000000")
        self.mocks({"v": 50})
        S.call(c, "read", 0)
        S.call(c, "read", 1)
        # a huge jump on meter 0 only
        self.mocks({"v": 900000})
        S.call(c, "read", 0)
        assert c.latest(0)["accepted"] is False
        # meter 1 still measures against its own accepted 50
        self.mocks({"v": 55})
        S.call(c, "read", 1)
        assert c.latest(1)["accepted"] is True

    # -- validation --------------------------------------------------------

    @pytest.mark.parametrize(
        "names,tols,guards",
        [
            ("x", "roughly", ""),                  # unknown mode
            ("x", "abs", ""),                      # abs without a parameter
            ("x", "pct:-1", ""),                   # negative tolerance
            ("x", "band:1000,100", ""),            # unsorted band
            ("x", "band:100,100", ""),             # duplicate edges
            ("x|x", "exact|exact", "|"),  # duplicate names
            (["", ], ["exact"], [""]),                   # empty name
            ("a|b", "exact", ""),               # length mismatch
            ("x", "exact", "wobble:3"),            # unknown guard
            ("x", "exact", "step:"),               # step without a number
            ("x", "exact", "step:-5"),             # negative step
            ("x", "exact", "range:100"),           # one sided range
            ("x", "exact", "range:100,0"),         # inverted range
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
            S.call(c, "define", "m", "ftp://a.example/s", "x", "exact", "")

    def test_too_many_fields_is_refused(self):
        c = S.deploy("contracts/tolerance.py")
        n = "|".join(f"f{i}" for i in range(20))
        with pytest.raises(S.UserError):
            S.call(c, "define", "m", "https://a.example/s", n,
                   "|".join(["exact"] * 20), "|" * 19)

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
                   "a|b", "exact|nonsense", "|")
        assert c.count() == 0

# ===========================================================================
# GenVM storage rules
#
# These are not tests of the logic. They are tests of the SHAPE, and they exist
# because two deployments failed on it: a storage dataclass cannot contain a
# DynArray, `list` and `int` are not valid storage types, and
# gl.storage.inmem_allocate is for generic dataclasses rather than collections.
#
# The simulator now refuses all three the way GenVM does, so importing the
# contract is itself the check. These make the intent explicit.
# ===========================================================================

class TestStorageShape:
    def test_the_contract_imports_under_genvm_storage_rules(self):
        """If the module loads, no dataclass holds a collection and no field
        uses a forbidden type. The simulator raises at class definition time."""
        import glsim as _S
        mod = _S.load_contract(CONTRACT_PATH)
        assert hasattr(mod, "Contract")

    def test_no_storage_dataclass_holds_a_collection(self):
        import ast, pathlib
        tree = ast.parse(pathlib.Path(CONTRACT_PATH).read_text())
        for cls in [x for x in tree.body if isinstance(x, ast.ClassDef)]:
            decs = " ".join(ast.unparse(d) for d in cls.decorator_list)
            if "allow_storage" not in decs:
                continue
            for st in cls.body:
                if isinstance(st, ast.AnnAssign):
                    ann = ast.unparse(st.annotation)
                    assert "DynArray" not in ann and "TreeMap" not in ann, (
                        f"{cls.name}.{ast.unparse(st.target)} nests {ann}"
                    )

    def test_no_forbidden_storage_types(self):
        import ast, pathlib
        tree = ast.parse(pathlib.Path(CONTRACT_PATH).read_text())
        for cls in [x for x in tree.body if isinstance(x, ast.ClassDef)]:
            decs = " ".join(ast.unparse(d) for d in cls.decorator_list)
            is_contract = any("gl.Contract" in ast.unparse(b) for b in cls.bases)
            if "allow_storage" not in decs and not is_contract:
                continue
            for st in cls.body:
                if isinstance(st, ast.AnnAssign):
                    ann = ast.unparse(st.annotation)
                    assert ann not in ("int", "float", "list", "dict", "tuple"), (
                        f"{cls.name}.{ast.unparse(st.target)}: {ann} is forbidden"
                    )
                    assert not ann.startswith(("list[", "dict[", "tuple[")), (
                        f"{cls.name}.{ast.unparse(st.target)}: {ann} is forbidden"
                    )

    def test_no_public_method_takes_a_builtin_container(self):
        """A calldata parameter typed list[str] sits close enough to the
        forbidden-list boundary to be a bet rather than a decision."""
        import ast, pathlib
        tree = ast.parse(pathlib.Path(CONTRACT_PATH).read_text())
        safe = {"str", "u256", "u8", "bool", "Address", "bytes"}
        for cls in [x for x in tree.body if isinstance(x, ast.ClassDef)]:
            for m in [x for x in cls.body if isinstance(x, ast.FunctionDef)]:
                if not any("gl.public" in ast.unparse(d) for d in m.decorator_list):
                    continue
                for a in m.args.args[1:]:
                    ann = ast.unparse(a.annotation) if a.annotation else "?"
                    assert ann in safe, f"{m.name}({a.arg}: {ann})"

    def test_every_persistent_field_is_declared_in_the_class_body(self):
        """A field created with self.x = value and never declared is NOT
        persistent. It is silently discarded when execution ends, so the
        contract appears to work and loses the data.

        Nothing warns about this. A static check is the only defence.
        """
        import ast, pathlib
        tree = ast.parse(pathlib.Path(CONTRACT_PATH).read_text())
        cls = [x for x in tree.body if isinstance(x, ast.ClassDef)
               and any("gl.Contract" in ast.unparse(b) for b in x.bases)][0]
        declared = {st.target.id for st in cls.body
                    if isinstance(st, ast.AnnAssign)}
        for m in [x for x in cls.body if isinstance(x, ast.FunctionDef)]:
            for node in ast.walk(m):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AugAssign):
                    targets = [node.target]
                for tg in targets:
                    if (isinstance(tg, ast.Attribute)
                            and isinstance(tg.value, ast.Name)
                            and tg.value.id == "self"):
                        assert tg.attr in declared, (
                            f"{m.name} assigns self.{tg.attr}, which is not "
                            f"declared in the class body and will not persist"
                        )

    def test_no_block_closes_over_a_storage_object(self):
        """Non-deterministic blocks cannot read storage at all.

        Everything a block needs must be extracted to a plain value first, or
        copied with gl.storage.copy_to_memory(). This asserts that every name a
        block closes over from the ENCLOSING scope was bound from a plain
        expression rather than straight off self.
        """
        import ast, pathlib
        tree = ast.parse(pathlib.Path(CONTRACT_PATH).read_text())
        for m in [x for x in ast.walk(tree) if isinstance(x, ast.FunctionDef)]:
            blocks = [b for b in ast.walk(m)
                      if isinstance(b, ast.FunctionDef)
                      and b.name in ("leader_fn", "validator_fn")]
            if not blocks:
                continue

            # names the enclosing method binds before the first block
            outer = {}
            for node in m.body:
                if isinstance(node, ast.FunctionDef):
                    break
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                    outer[node.targets[0].id] = ast.unparse(node.value)

            for b in blocks:
                # names the block binds itself are local, not closed over
                local = {t.id for n in ast.walk(b)
                         if isinstance(n, ast.Assign)
                         for t in n.targets if isinstance(t, ast.Name)}
                local |= {a.arg for a in b.args.args}
                for x in ast.walk(b):
                    if not isinstance(x, ast.Name) or x.id not in outer:
                        continue
                    if x.id in local:
                        continue
                    expr = outer[x.id]
                    plain = (expr.startswith(("str(", "int(", "float(", "bool(", "["))
                             or "copy_to_memory" in expr)
                    assert plain, (
                        f"{m.name}: the block closes over `{x.id} = {expr}`, "
                        f"which may be a live storage object"
                    )

    def test_no_storage_field_is_declared_twice(self):
        """A duplicated field annotation is silent in Python and is not
        something to hand to a storage layout builder.

        This is not hypothetical. An editing mistake left

            claims: DynArray[Claim]
            checks: DynArray[Check]
            checks: DynArray[Check]

        in this contract, and every behavioural test still passed, because
        Python simply keeps the last annotation and carries on.
        """
        import ast, collections, pathlib
        tree = ast.parse(pathlib.Path(CONTRACT_PATH).read_text())
        for cls in [x for x in tree.body if isinstance(x, ast.ClassDef)]:
            names = [st.target.id for st in cls.body
                     if isinstance(st, ast.AnnAssign)
                     and isinstance(st.target, ast.Name)]
            dupes = [n for n, c in collections.Counter(names).items() if c > 1]
            assert not dupes, f"{cls.name} declares {dupes} more than once"

    def test_no_method_is_defined_twice(self):
        """A duplicated method silently shadows the first one.

        This is not hypothetical: an editing mistake left two definitions of a
        lookup helper in this contract, and the second, unmutated copy made a
        mutation test pass that should have failed. Python allows it and says
        nothing at all.
        """
        import ast, collections, pathlib
        tree = ast.parse(pathlib.Path(CONTRACT_PATH).read_text())
        for cls in [x for x in tree.body if isinstance(x, ast.ClassDef)]:
            names = [m.name for m in cls.body if isinstance(m, ast.FunctionDef)]
            dupes = [n for n, c in collections.Counter(names).items() if c > 1]
            assert not dupes, f"{cls.name} defines {dupes} more than once"

    def test_no_top_level_name_is_defined_twice(self):
        import ast, collections, pathlib
        tree = ast.parse(pathlib.Path(CONTRACT_PATH).read_text())
        names = [x.name for x in tree.body
                 if isinstance(x, (ast.FunctionDef, ast.ClassDef))]
        dupes = [n for n, c in collections.Counter(names).items() if c > 1]
        assert not dupes, f"module defines {dupes} more than once"

    def test_inmem_allocate_is_not_used_on_a_collection(self):
        import pathlib, re
        src = pathlib.Path(CONTRACT_PATH).read_text()
        for line in src.splitlines():
            if line.strip().startswith("#"):
                continue
            assert not re.search(r"inmem_allocate\(\s*(DynArray|TreeMap)", line), line
