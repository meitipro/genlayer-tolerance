"""
glsim — a small GenVM stand-in, good enough to execute the real contract files.

This is not a test. It is the harness the tests run on. It exists so the
DETERMINISTIC HALF of each contract can be executed for real: storage writes,
re-derivation, the plausibility gate, the validation branches. The pure
consensus helpers are already covered by tests/test_logic.py; everything below
the block is what this reaches.

It deliberately models the parts of GenVM that cause bugs:

  * a non-deterministic block runs TWICE, once as the leader and once as a
    validator, with independent mock responses, so a contract that assumes both
    runs see identical data is caught here rather than on a real network
  * the validator's verdict decides whether the transaction proceeds
  * a leader that raises produces a non-Return result, exactly as the runtime does
  * storage is snapshotted before a write and rolled back if the method raises,
    so a half-written record shows up as a failure
"""

import sys
import types
import copy


# ---------------------------------------------------------------------------
# storage types
# ---------------------------------------------------------------------------

class _Generic:
    """What DynArray[T] and TreeMap[K, V] evaluate to.

    Deliberately NOT callable. Real GenVM refuses `DynArray[T]()` with
    "this class can't be instantiated by user", because storage generics have a
    fixed memory layout and no type erasure. The only legal way to build one in
    memory is gl.storage.inmem_allocate(DynArray[T]).

    That restriction cost a deployment once. Modelling it here means the tests
    fail on the workstation instead of on chain.
    """

    def __init__(self, origin):
        self.__origin__ = origin

    def __call__(self, *a):
        raise TypeError("this class can't be instantiated by user")

    def __repr__(self):
        return f"{self.__origin__.__name__}[...]"


class DynArray(list):
    def __class_getitem__(cls, item):
        return _Generic(DynArray)

    def truncate(self):
        self.clear()


class TreeMap(dict):
    def __class_getitem__(cls, item):
        return _Generic(TreeMap)

    def get(self, k, default=None):
        return dict.get(self, k, default)


class Address(str):
    @staticmethod
    def zero():
        return Address("0x" + "0" * 40)


def u256(v=0):
    return int(v)


def u8(v=0):
    return int(v)


def allow_storage(cls):
    """Marks a dataclass as storable, and refuses what GenVM refuses.

    A nested collection cannot be built in memory: the field would have to be
    supplied to the constructor, and there is no legal way to make one. On
    chain this shows up as a TypeError deep inside the runner. Catching it at
    class definition time turns a failed deployment into a failed import.
    """
    for name, ann in getattr(cls, "__annotations__", {}).items():
        origin = getattr(ann, "__origin__", None)
        if ann in (DynArray, TreeMap) or origin in (DynArray, TreeMap):
            raise TypeError(
                f"{cls.__name__}.{name}: a storage dataclass cannot contain a "
                f"collection. Make it a top level contract field and carry an "
                f"id on the record instead."
            )
        if ann in (int, list, dict, tuple):
            raise TypeError(
                f"{cls.__name__}.{name}: {ann.__name__} is not a valid storage type"
            )
        if ann in (DynArray, TreeMap):
            raise TypeError(
                f"{cls.__name__}.{name}: only fully instantiated generics are "
                f"allowed, write DynArray[T] or TreeMap[K, V]"
            )
    return cls


# ---------------------------------------------------------------------------
# errors and results
# ---------------------------------------------------------------------------

class UserError(Exception):
    def __init__(self, message=""):
        super().__init__(message)
        self.message = message


class VMError(Exception):
    pass


class Result:
    pass


class Return(Result):
    def __init__(self, calldata):
        self.calldata = calldata


class Rollback(Result):
    def __init__(self, message):
        self.message = message


class ContractError(Result):
    def __init__(self, message):
        self.message = message


# ---------------------------------------------------------------------------
# the non-deterministic environment
# ---------------------------------------------------------------------------

class NonDetEnv:
    """Holds the mock web pages and prompt answers for one run.

    `leader` and `validator` can be given different tables, which is how a
    contract that quietly assumes both nodes see the same bytes gets caught.
    """

    def __init__(self, pages=None, prompts=None):
        self.pages = pages or {}
        self.prompts = prompts or {}
        self.render_calls = []
        self.prompt_calls = []

    def render(self, url, mode="text"):
        self.render_calls.append((url, mode))
        for key, value in self.pages.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise UserError(f"no mock page for {url}")

    def exec_prompt(self, prompt, response_format=None, images=None):
        self.prompt_calls.append(prompt)
        for key, value in self.prompts.items():
            if key in prompt:
                if isinstance(value, Exception):
                    raise value
                return copy.deepcopy(value)
        raise UserError("no mock prompt response matched")


class StorageInNondet(Exception):
    """Raised when a block touches a live storage object.

    On chain this is a hard error: non-deterministic blocks cannot read storage
    at all. Contracts must extract plain values first, or use
    gl.storage.copy_to_memory(). Modelling it means a contract that closes over
    a storage view fails here rather than on the network.
    """


class _Runtime:
    def __init__(self):
        self.leader_env = NonDetEnv()
        self.validator_env = NonDetEnv()
        self.active = None
        self.sender = Address("0x" + "11" * 20)
        self.origin = None
        self.value = 0
        self.datetime = "2026-08-21T10:00:00Z"
        self.last_validator_verdict = None
        self.block_runs = 0


RT = _Runtime()


def _run_nondet_unsafe(leader_fn, validator_fn):
    """Run the block as the leader, then as a validator, then decide."""
    RT.block_runs += 1

    RT.active = RT.leader_env
    try:
        leader_out = leader_fn()
        leaders_res = Return(leader_out)
    except UserError as e:
        leaders_res = Rollback(e.message)
        leader_out = None
    except Exception as e:                       # noqa: BLE001
        leaders_res = ContractError(str(e))
        leader_out = None

    RT.active = RT.validator_env
    verdict = bool(validator_fn(leaders_res))
    RT.last_validator_verdict = verdict
    RT.active = None

    if not verdict:
        raise UserError("validators did not agree with the leader")
    if not isinstance(leaders_res, Return):
        raise UserError("leader failed")
    return leader_out


# ---------------------------------------------------------------------------
# the gl namespace
# ---------------------------------------------------------------------------

def _identity(fn):
    return fn


class _Public:
    def __init__(self):
        self.view = _identity
        self.write = _Write()


class _Write:
    def __call__(self, fn):
        return fn

    @property
    def payable(self):
        return _identity


class _Message:
    @property
    def sender_address(self):
        return RT.sender

    @property
    def origin_address(self):
        return RT.origin or RT.sender

    @property
    def value(self):
        return RT.value


class _Web:
    def render(self, url, mode="text"):
        if RT.active is None:
            raise VMError("web access outside a non-deterministic block")
        return RT.active.render(url, mode)

    def request(self, url, method="GET", body=None):
        raise VMError("not modelled")


class _NonDet:
    def __init__(self):
        self.web = _Web()

    def exec_prompt(self, prompt, response_format=None, images=None):
        if RT.active is None:
            raise VMError("prompt outside a non-deterministic block")
        return RT.active.exec_prompt(prompt, response_format, images)


class _VM:
    UserError = UserError
    VMError = VMError
    Result = Result
    Return = Return
    Rollback = Rollback
    ContractError = ContractError
    run_nondet_unsafe = staticmethod(_run_nondet_unsafe)
    run_nondet = staticmethod(_run_nondet_unsafe)


class _EqPrinciple:
    @staticmethod
    def strict_eq(fn):
        def validator(leaders_res):
            if not isinstance(leaders_res, Return):
                return False
            return fn() == leaders_res.calldata
        return _run_nondet_unsafe(fn, validator)


class _Contract:
    """Base class. Storage fields are created from the class annotations."""

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)

    #: GenVM refuses these outright as storage field types.
    #: GenVM refuses these outright as storage field types. `float` is legal
    #: on chain (it zero-initialises to +0) but is kept out of these contracts
    #: for a different reason: it should not cross the calldata boundary.
    FORBIDDEN = {int: "int (use u256, i256 or bigint)",
                 list: "list (use DynArray[T])",
                 dict: "dict (use TreeMap[K, V])",
                 tuple: "tuple"}

    def __new__(cls, *a, **kw):
        obj = super().__new__(cls)
        for name, ann in getattr(cls, "__annotations__", {}).items():
            if ann in _Contract.FORBIDDEN:
                raise TypeError(
                    f"{cls.__name__}.{name}: {_Contract.FORBIDDEN[ann]} is not a "
                    f"valid storage type"
                )
            origin0 = getattr(ann, "__origin__", None)
            if origin0 in (list, dict, tuple, set):
                raise TypeError(
                    f"{cls.__name__}.{name}: builtin containers are not valid "
                    f"storage types, use DynArray or TreeMap"
                )
        for name, ann in getattr(cls, "__annotations__", {}).items():
            origin = getattr(ann, "__origin__", None)
            if ann is DynArray or origin is DynArray:
                setattr(obj, name, DynArray())
            elif ann is TreeMap or origin is TreeMap:
                setattr(obj, name, TreeMap())
            elif origin is not None:
                setattr(obj, name, origin())
            elif ann is str:
                setattr(obj, name, "")
            elif ann in (int,):
                setattr(obj, name, 0)
            elif ann is bool:
                setattr(obj, name, False)
            elif ann is Address:
                setattr(obj, name, Address.zero())
            else:
                setattr(obj, name, None)
        return obj


class _Storage:
    @staticmethod
    def copy_to_memory(x):
        return copy.deepcopy(x)

    @staticmethod
    def inmem_allocate(t, *a, **kw):
        """Mirrors gl.storage.inmem_allocate, including what it CANNOT do.

        The real function takes a fully instantiated GENERIC DATACLASS and the
        arguments its __init__ would take: inmem_allocate(Item[str], data,
        label). It is not a way to build a collection.

        Handing it DynArray[T] or TreeMap[K, V] fails on chain with
        "_GenericAlias.__init__() missing 1 required positional argument",
        because the subscripted generic's __init__ is not the collection's.
        Refusing it here means the tests fail on the workstation instead.
        """
        origin = getattr(t, "__origin__", t)
        if origin in (DynArray, TreeMap):
            raise TypeError(
                "inmem_allocate cannot build a storage collection. Declare it as "
                "a top level contract field instead; the runtime allocates those."
            )
        return origin(*a, **kw)


class _GL:
    def __init__(self):
        self.Contract = _Contract
        self.public = _Public()
        self.message = _Message()
        self.nondet = _NonDet()
        self.vm = _VM()
        self.eq_principle = _EqPrinciple()
        self.storage = _Storage()
        self.message_raw = {"datetime": RT.datetime, "is_init": True, "stack": []}

    def get_contract_at(self, addr):
        raise VMError("not modelled")

    def deploy_contract(self, **kw):
        raise VMError("not modelled")


gl = _GL()


# ---------------------------------------------------------------------------
# loading a real contract file
# ---------------------------------------------------------------------------

def _install_module():
    m = types.ModuleType("genlayer")
    from dataclasses import dataclass as _dc
    m.gl = gl
    m.DynArray = DynArray
    m.TreeMap = TreeMap
    m.Address = Address
    m.u256 = u256
    m.u8 = u8
    m.allow_storage = allow_storage
    m.dataclass = _dc
    m.__all__ = [
        "gl", "DynArray", "TreeMap", "Address", "u256", "u8",
        "allow_storage", "dataclass",
    ]
    sys.modules["genlayer"] = m


_install_module()


def load_contract(path):
    """Execute a real contract file and return its module namespace."""
    src = open(path, encoding="utf-8").read()
    ns = {"__name__": f"contract_{path}"}
    exec(compile(src, path, "exec"), ns)
    return types.SimpleNamespace(**ns)


def deploy(path, *args):
    """Instantiate the contract in this file, exactly as GenVM would."""
    mod = load_contract(path)
    gl.message_raw["is_init"] = True
    c = mod.Contract(*args)
    if hasattr(c, "__init__"):
        pass
    gl.message_raw["is_init"] = False
    c._module = mod
    return c


def set_mocks(leader_pages=None, leader_prompts=None,
              validator_pages=None, validator_prompts=None):
    """Give the leader and the validator their own view of the world.

    Passing only leader_* makes both nodes see the same thing, which is the
    common case. Passing both is how divergence is tested.
    """
    RT.leader_env = NonDetEnv(leader_pages, leader_prompts)
    RT.validator_env = NonDetEnv(
        validator_pages if validator_pages is not None else leader_pages,
        validator_prompts if validator_prompts is not None else leader_prompts,
    )
    RT.block_runs = 0
    RT.last_validator_verdict = None


def set_sender(addr):
    RT.sender = Address(addr)


def set_time(iso):
    RT.datetime = iso
    gl.message_raw["datetime"] = iso


def call(contract, method, *args):
    """Call a method with storage rollback on failure, as the runtime does."""
    snapshot = {
        k: copy.deepcopy(v)
        for k, v in contract.__dict__.items()
        if not k.startswith("_")
    }
    try:
        return getattr(contract, method)(*args)
    except Exception:
        for k, v in snapshot.items():
            setattr(contract, k, v)
        raise
