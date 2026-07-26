import importlib.util
import builtins
import copy
import functools
import inspect
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pandas as pd

from jq_tushare_sdk.api import jqdata
from jq_tushare_sdk.api.globals import exported_globals, set_runtime_state

_MISSING = object()
_ROLLBACK_UNAVAILABLE = object()


class _LegacyPandasPanel:
    pass


@contextmanager
def _runtime_state_scope(state):
    try:
        previous_state = jqdata.runtime_state()
        restore_uninitialized = False
    except RuntimeError:
        previous_state = None
        restore_uninitialized = True

    set_runtime_state(state)
    try:
        yield
    finally:
        if restore_uninitialized:
            jqdata._STATE = None
        else:
            set_runtime_state(previous_state)


@dataclass
class LoadedStrategy:
    path: Path
    module: ModuleType
    namespace: dict
    module_slots: dict[str, ModuleType]
    state: Any
    loaded_run_type: str
    safety: Any = None

    def _run_lifecycle_hook(self, name, context, required=False):
        callback = self.namespace.get(name)
        if callback is None:
            if required:
                raise AttributeError(f"Strategy {self.path} does not define {name}(context)")
            return None
        _assert_strategy_run_mode(
            self.loaded_run_type,
            context,
            self.path,
            name,
        )
        self.state.context = context
        if self.safety is None:
            with (
                _runtime_state_scope(self.state),
                _temporary_module_slots(self.module_slots),
                _strategy_import_path(self.path),
                _pandas_legacy_panel_guard(),
            ):
                return callback(context)
        with _runtime_state_scope(self.state):
            return self.safety.execute(callback, context)

    def initialize(self, context):
        return self._run_lifecycle_hook("initialize", context, required=True)

    def after_code_changed(self, context):
        """Match JoinQuant backtests, which invoke this optional hook once."""
        return self._run_lifecycle_hook("after_code_changed", context)

    def process_initialize(self, context):
        """Initialize non-persistent runtime resources after other startup hooks."""
        return self._run_lifecycle_hook("process_initialize", context)


class InertRedisClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.closed = False
        self.operations: list[tuple[str, tuple, dict]] = []

    def ping(self):
        self.operations.append(("ping", (), {}))
        return True

    def close(self):
        self.closed = True
        self.operations.append(("close", (), {}))

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            self.operations.append((name, args, kwargs))
            return None

        return _method


def _make_inert_redis_module() -> ModuleType:
    module = ModuleType("redis")

    class ConnectionError(Exception):
        pass

    module.Redis = InertRedisClient
    module.StrictRedis = InertRedisClient
    module.ConnectionError = ConnectionError
    module.exceptions = SimpleNamespace(ConnectionError=ConnectionError)
    module.from_url = lambda *args, **kwargs: InertRedisClient(*args, **kwargs)
    module.__all__ = ["Redis", "StrictRedis", "ConnectionError", "from_url"]
    return module


def _strategy_sibling_module_names(path: str | Path) -> set[str]:
    strategy_path = Path(path)
    excluded_names = {"__init__", "jqdata", strategy_path.stem}
    candidates: set[str] = set()
    for entry in strategy_path.parent.iterdir():
        if entry.is_file() and entry.suffix == ".py" and entry.stem not in excluded_names:
            candidates.add(entry.stem)
            continue
        if entry.is_dir() and entry.name not in excluded_names and (entry / "__init__.py").is_file():
            candidates.add(entry.name)
    return candidates


def _strategy_scoped_module_keys(candidate_names: set[str]) -> set[str]:
    if not candidate_names:
        return set()
    return {
        module_name
        for module_name in list(sys.modules)
        if module_name.split(".", 1)[0] in candidate_names
    }


@contextmanager
def _strategy_import_path(path: str | Path):
    strategy_path = Path(path)
    strategy_dir = str(strategy_path.parent)
    original_sys_path = list(sys.path)
    candidate_names = _strategy_sibling_module_names(strategy_path)
    original_module_keys = _strategy_scoped_module_keys(candidate_names)
    original_modules = {module_name: sys.modules[module_name] for module_name in original_module_keys}
    sys.path[:] = [entry for entry in original_sys_path if entry != strategy_dir]
    sys.path.insert(0, strategy_dir)
    for module_name in original_module_keys:
        sys.modules.pop(module_name, None)
    try:
        yield
    finally:
        for module_name in _strategy_scoped_module_keys(candidate_names):
            sys.modules.pop(module_name, None)
        sys.modules.update(original_modules)
        sys.path[:] = original_sys_path


def _strategy_callback_wrapper(
    path: str | Path,
    module_slots: dict[str, ModuleType],
    state,
    loaded_run_type: str,
):
    def _wrap(func):
        @functools.wraps(func)
        def _wrapped(context, *args, **kwargs):
            _assert_strategy_run_mode(
                loaded_run_type,
                context,
                path,
                "execute scheduled callback",
            )
            state.context = context
            with (
                _runtime_state_scope(state),
                _temporary_module_slots(module_slots),
                _strategy_import_path(path),
                _pandas_legacy_panel_guard(),
            ):
                return func(context, *args, **kwargs)

        return _wrapped

    return _wrap


def _restore_namespace_attributes(namespace, snapshot: dict[str, Any] | None):
    if snapshot is None or namespace is None:
        return
    try:
        current = vars(namespace)
    except TypeError:
        return
    current.clear()
    current.update(snapshot)


def _restore_scheduler_attribute(scheduler, attribute_name: str, snapshot):
    if scheduler is None:
        return
    if snapshot is _MISSING:
        if hasattr(scheduler, attribute_name):
            try:
                delattr(scheduler, attribute_name)
            except Exception as exc:
                error = RuntimeError(
                    f"Cannot restore scheduler.{attribute_name} after failed strategy load"
                )
                raise error from exc
        return
    try:
        setattr(scheduler, attribute_name, snapshot)
    except Exception as setattr_error:
        if attribute_name == "entries":
            try:
                entries = getattr(scheduler, attribute_name)
                entries[:] = snapshot
                return
            except Exception as slice_error:
                error = RuntimeError(
                    "Cannot restore scheduler.entries after failed strategy load"
                )
                error.add_note(f"setattr failed first: {setattr_error!r}")
                raise error from slice_error
        error = RuntimeError(
            f"Cannot restore scheduler.{attribute_name} after failed strategy load"
        )
        raise error from setattr_error


def _restore_records(state, snapshot):
    current_records = getattr(state, "records", None)
    if current_records is None:
        setattr(state, "records", snapshot)
        return
    try:
        current_records[:] = snapshot
    except Exception as slice_error:
        try:
            setattr(state, "records", snapshot)
        except Exception as assign_error:
            error = RuntimeError("Cannot restore runtime records after failed strategy load")
            error.add_note(f"slice restore failed first: {slice_error!r}")
            raise error from assign_error


def _rollback_snapshot_failure(message: str, cause: Exception | None):
    error = RuntimeError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


@contextmanager
def _state_import_side_effect_guard(state):
    g_snapshot = None
    g_snapshot_error = None
    g_state = getattr(state, "g", None)
    if g_state is not None:
        try:
            g_snapshot = copy.deepcopy(vars(g_state))
        except Exception as exc:
            g_snapshot = _ROLLBACK_UNAVAILABLE
            g_snapshot_error = exc

    records_snapshot = None
    records_snapshot_error = None
    if hasattr(state, "records"):
        try:
            records_snapshot = copy.deepcopy(getattr(state, "records"))
        except Exception as exc:
            records_snapshot = _ROLLBACK_UNAVAILABLE
            records_snapshot_error = exc

    scheduler = getattr(state, "scheduler", None)
    scheduler_entries_snapshot = _MISSING
    scheduler_wrapper_snapshot = _MISSING
    if scheduler is not None:
        if hasattr(scheduler, "entries"):
            try:
                scheduler_entries_snapshot = list(getattr(scheduler, "entries"))
            except Exception:
                scheduler_entries_snapshot = _MISSING
        if hasattr(scheduler, "_callback_wrapper"):
            try:
                scheduler_wrapper_snapshot = getattr(scheduler, "_callback_wrapper")
            except Exception:
                scheduler_wrapper_snapshot = _MISSING

    try:
        yield
    except Exception as original_error:
        rollback_failures: list[Exception] = []
        try:
            _restore_scheduler_attribute(
                scheduler,
                "entries",
                list(scheduler_entries_snapshot)
                if scheduler_entries_snapshot is not _MISSING
                else _MISSING,
            )
        except Exception as exc:
            rollback_failures.append(exc)
        try:
            _restore_scheduler_attribute(scheduler, "_callback_wrapper", scheduler_wrapper_snapshot)
        except Exception as exc:
            rollback_failures.append(exc)
        if records_snapshot is _ROLLBACK_UNAVAILABLE:
            rollback_failures.append(
                _rollback_snapshot_failure(
                    "Cannot safely roll back runtime records after failed strategy load: "
                    "deep snapshot of RuntimeState.records failed",
                    records_snapshot_error,
                )
            )
        else:
            try:
                _restore_records(state, records_snapshot)
            except Exception as exc:
                rollback_failures.append(exc)
        if g_snapshot is _ROLLBACK_UNAVAILABLE:
            rollback_failures.append(
                _rollback_snapshot_failure(
                    "Cannot safely roll back state.g after failed strategy load: "
                    "deep snapshot of runtime g failed",
                    g_snapshot_error,
                )
            )
        else:
            try:
                _restore_namespace_attributes(g_state, g_snapshot)
            except Exception as exc:
                rollback_failures.append(exc)
        if rollback_failures:
            raise ExceptionGroup(
                f"Strategy load failed and rollback was incomplete: {original_error}",
                [original_error, *rollback_failures],
            ) from None
        raise


@contextmanager
def _temporary_module_slots(entries: dict[str, ModuleType]):
    original_entries = {
        module_name: sys.modules.get(module_name, _MISSING)
        for module_name in entries
    }
    sys.modules.update(entries)
    try:
        yield
    finally:
        for module_name, original_value in original_entries.items():
            if original_value is _MISSING:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = original_value


@contextmanager
def _pandas_legacy_panel_guard():
    if hasattr(pd, "Panel"):
        yield
        return

    pd.Panel = _LegacyPandasPanel
    try:
        yield
    finally:
        if getattr(pd, "Panel", None) is _LegacyPandasPanel:
            delattr(pd, "Panel")


def _runtime_mode(context) -> str:
    if context is None:
        return "backtest"
    run_params = getattr(context, "run_params", None)
    return getattr(run_params, "type", "backtest")


def _assert_strategy_run_mode(expected_run_type: str, context, strategy_path: str | Path, action: str):
    actual_run_type = _runtime_mode(context)
    if actual_run_type != expected_run_type:
        raise RuntimeError(
            f"Strategy {Path(strategy_path)} loaded under run mode {expected_run_type!r} "
            f"cannot {action} under run mode {actual_run_type!r}"
        )


def _is_backtest_mode(context) -> bool:
    return _runtime_mode(context) == "backtest"


def _force_backtest_send_flag(init_func, args, kwargs):
    try:
        signature = inspect.signature(init_func)
    except (TypeError, ValueError):
        return tuple(args), dict(kwargs)

    if "send_redis_signal" not in signature.parameters:
        return tuple(args), dict(kwargs)

    try:
        bound = signature.bind_partial(None, *args, **kwargs)
    except TypeError:
        forced_kwargs = dict(kwargs)
        send_param = signature.parameters["send_redis_signal"]
        if send_param.kind is not inspect.Parameter.POSITIONAL_ONLY:
            forced_kwargs["send_redis_signal"] = False
        return tuple(args), forced_kwargs

    bound.arguments["send_redis_signal"] = False
    rebound_args = tuple(bound.args[1:])
    rebound_kwargs = dict(bound.kwargs)
    return rebound_args, rebound_kwargs


def _is_inert_redis_client(value) -> bool:
    return isinstance(value, InertRedisClient)


class BacktestRuntimeSafety:
    _MAX_WALK_OBJECTS = 2048

    def __init__(self, state, strategy_path: str | Path, loaded_run_type: str = "backtest"):
        self.state = state
        self.strategy_path = Path(strategy_path)
        self.loaded_run_type = loaded_run_type
        self.inert_redis_module = _make_inert_redis_module()
        self.module: ModuleType | None = None
        self.module_slots: dict[str, ModuleType] = {}
        self.wrapper_classes: list[type] = []

    @contextmanager
    def redis_guard(self):
        original_redis_module = sys.modules.get("redis")
        sys.modules["redis"] = self.inert_redis_module
        try:
            yield self.inert_redis_module
        finally:
            if original_redis_module is None:
                sys.modules.pop("redis", None)
            else:
                sys.modules["redis"] = original_redis_module

    def wrap_callback(self, func):
        @functools.wraps(func)
        def _wrapped(context, *args, **kwargs):
            _assert_strategy_run_mode(
                self.loaded_run_type,
                context,
                self.strategy_path,
                "execute scheduled callback",
            )
            return self.execute(func, context, *args, **kwargs)

        return _wrapped

    def execute(self, func, context, *args, **kwargs):
        self.state.context = context
        with (
            _runtime_state_scope(self.state),
            _temporary_module_slots(self.module_slots),
            _strategy_import_path(self.strategy_path),
            _pandas_legacy_panel_guard(),
            self.redis_guard(),
            self.class_definition_guard(),
        ):
            try:
                return func(context, *args, **kwargs)
            finally:
                active_exception = sys.exc_info()[1]
                try:
                    self.sanitize_runtime_objects()
                except Exception:
                    if active_exception is None:
                        raise
                    logger = getattr(self.state, "log", None)
                    log_error = getattr(logger, "error", None)
                    if callable(log_error):
                        try:
                            log_error(
                                "Suppressed runtime safety cleanup error while preserving original strategy exception",
                                exc_info=True,
                            )
                        except Exception:
                            pass

    def patch_module(self, module: ModuleType):
        self.module = module
        self._patch_target_portfolio_module_functions(module)
        self.sanitize_runtime_objects()

    def set_module_slots(self, module_slots: dict[str, ModuleType]):
        self.module_slots = dict(module_slots)

    @contextmanager
    def class_definition_guard(self):
        original_build_class = builtins.__build_class__

        @functools.wraps(original_build_class)
        def _safe_build_class(func, name, *bases, **kwargs):
            cls = original_build_class(func, name, *bases, **kwargs)
            if self._is_wrapper_class(cls):
                self._patch_wrapper_class(cls)
            return cls

        builtins.__build_class__ = _safe_build_class
        try:
            yield
        finally:
            builtins.__build_class__ = original_build_class

    def _raise_walk_budget_exceeded(self, root_location: str, current_location: str):
        raise RuntimeError(
            "Backtest runtime safety walk budget exceeded while inspecting "
            f"{root_location} at {current_location} after {self._MAX_WALK_OBJECTS} objects"
        )

    def _patch_wrapper_class(self, wrapper_cls: type):
        if getattr(wrapper_cls, "__jqts_backtest_safe__", False):
            if wrapper_cls not in self.wrapper_classes:
                self.wrapper_classes.append(wrapper_cls)
            return

        original_init = wrapper_cls.__init__

        @functools.wraps(original_init)
        def _safe_init(instance, *args, **kwargs):
            safe_args, safe_kwargs = _force_backtest_send_flag(original_init, args, kwargs)
            original_init(instance, *safe_args, **safe_kwargs)
            self._sanitize_wrapper_instance(instance, f"{wrapper_cls.__name__}()")

        wrapper_cls.__init__ = _safe_init
        self._patch_target_portfolio_wrapper_methods(wrapper_cls)
        wrapper_cls.__jqts_backtest_safe__ = True
        self.wrapper_classes.append(wrapper_cls)

    def _patch_target_portfolio_wrapper_methods(self, wrapper_cls: type):
        for method_name in (
            "send_target_portfolio",
            "send_target_portfolio_map",
            "send_target_portfolio_from_plan",
            "send_redis_signal",
        ):
            method = getattr(wrapper_cls, method_name, None)
            if method is None or getattr(method, "__jqts_backtest_target_capture__", False):
                continue
            setattr(
                wrapper_cls,
                method_name,
                self._build_target_portfolio_wrapper_method(method_name, method),
            )

    def _build_target_portfolio_wrapper_method(self, method_name: str, method):
        if method_name == "send_redis_signal":
            @functools.wraps(method)
            def _wrapped(instance, signal_data, *args, **kwargs):
                if self._is_target_portfolio_signal(signal_data):
                    return self._capture_target_portfolio_signal(signal_data)
                return False

            _wrapped.__jqts_backtest_target_capture__ = True
            return _wrapped

        @functools.wraps(method)
        def _wrapped(instance, *args, **kwargs):
            original_flag = getattr(instance, "send_redis_signal_enabled", False)
            try:
                instance.send_redis_signal_enabled = True
            except Exception:
                return method(instance, *args, **kwargs)
            try:
                return method(instance, *args, **kwargs)
            finally:
                try:
                    instance.send_redis_signal_enabled = False
                except Exception:
                    if original_flag:
                        raise

        _wrapped.__jqts_backtest_target_capture__ = True
        return _wrapped

    def _patch_target_portfolio_module_functions(self, module: ModuleType):
        send_func = getattr(module, "send_ptrade_redis_signal", None)
        if send_func is None or getattr(send_func, "__jqts_backtest_target_capture__", False):
            return

        @functools.wraps(send_func)
        def _wrapped(context, signal):
            if self._is_target_portfolio_signal(signal):
                return self._capture_target_portfolio_signal(signal)
            return False

        _wrapped.__jqts_backtest_target_capture__ = True
        module.send_ptrade_redis_signal = _wrapped

    def _is_target_portfolio_signal(self, signal) -> bool:
        return isinstance(signal, dict) and signal.get("type") == "target_portfolio"

    def _capture_target_portfolio_signal(self, signal) -> bool:
        broker = getattr(self.state, "broker", None)
        capture = getattr(broker, "capture_target_portfolio", None)
        if not callable(capture):
            return False
        capture(signal)
        return True

    def sanitize_runtime_objects(self):
        self._patch_discovered_wrapper_classes()
        if self.module is not None:
            self._walk_runtime_object(self.module.__dict__, f"strategy {self.module.__name__}")
        g_state = getattr(self.state, "g", None)
        if g_state is not None:
            self._walk_runtime_object(vars(g_state), "runtime g")

    def _patch_discovered_wrapper_classes(self):
        if self.module is not None:
            for wrapper_cls, _location in self._discover_wrapper_classes(
                self.module.__dict__, f"strategy {self.module.__name__}"
            ):
                self._patch_wrapper_class(wrapper_cls)
        g_state = getattr(self.state, "g", None)
        if g_state is not None:
            for wrapper_cls, _location in self._discover_wrapper_classes(vars(g_state), "runtime g"):
                self._patch_wrapper_class(wrapper_cls)

    def _discover_wrapper_classes(self, root, location: str):
        discovered: list[tuple[type, str]] = []
        visited: set[int] = set()
        nodes_seen = 0

        def _walk(value, current_location: str):
            nonlocal nodes_seen
            if value is None:
                return
            value_id = id(value)
            if value_id in visited:
                return
            visited.add(value_id)
            nodes_seen += 1
            if nodes_seen > self._MAX_WALK_OBJECTS:
                self._raise_walk_budget_exceeded(location, current_location)
            if self._is_wrapper_class(value):
                discovered.append((value, current_location))
                return
            for child, child_location in self._iter_nested_objects(value, current_location):
                _walk(child, child_location)

        _walk(root, location)
        return discovered

    def _walk_runtime_object(self, root, location: str):
        visited: set[int] = set()
        nodes_seen = 0

        def _walk(value, current_location: str):
            nonlocal nodes_seen
            if value is None:
                return
            value_id = id(value)
            if value_id in visited:
                return
            visited.add(value_id)
            nodes_seen += 1
            if nodes_seen > self._MAX_WALK_OBJECTS:
                self._raise_walk_budget_exceeded(location, current_location)
            if self._looks_like_wrapper_instance(value):
                self._sanitize_wrapper_instance(value, current_location)
                return
            for child, child_location in self._iter_nested_objects(value, current_location):
                _walk(child, child_location)

        _walk(root, location)

    def _iter_nested_objects(self, value, location: str):
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if self.module is not None and value is self.module.__dict__ and str(key).startswith("__"):
                    continue
                yield child, f"{location}[{key!r}]"
            return

        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                yield child, f"{location}[{index}]"
            return

        if isinstance(value, (set, frozenset)):
            for index, child in enumerate(value):
                yield child, f"{location}{{{index}}}"
            return

        if inspect.isfunction(value):
            if getattr(value, "__jqts_backtest_target_capture__", False):
                return
            if getattr(value, "__module__", None) != getattr(self.module, "__name__", None):
                return
            defaults = getattr(value, "__defaults__", None) or ()
            for index, child in enumerate(defaults):
                yield child, f"{location}.__defaults__[{index}]"
            kwdefaults = getattr(value, "__kwdefaults__", None) or {}
            for name, child in kwdefaults.items():
                yield child, f"{location}.__kwdefaults__[{name!r}]"
            closure = getattr(value, "__closure__", None) or ()
            for index, cell in enumerate(closure):
                try:
                    child = cell.cell_contents
                except ValueError:
                    continue
                yield child, f"{location}.__closure__[{index}]"
            return

        if isinstance(value, ModuleType):
            if value is self.module:
                yield value.__dict__, f"{location}.__dict__"
            return

        namespace = getattr(value, "__dict__", None)
        if namespace is None:
            return
        if isinstance(value, SimpleNamespace):
            yield vars(value), f"{location}.__dict__"
            return
        yield namespace, f"{location}.__dict__"

    def _is_wrapper_class(self, value) -> bool:
        return isinstance(value, type) and value.__name__ == "JQOrderWrapper"

    def _looks_like_wrapper_instance(self, value) -> bool:
        if value is None or isinstance(value, type):
            return False
        if any(isinstance(value, wrapper_cls) for wrapper_cls in self.wrapper_classes):
            return True
        return value.__class__.__name__ == "JQOrderWrapper" and hasattr(
            value, "send_redis_signal_enabled"
        )

    def _sanitize_wrapper_instance(self, instance, location: str):
        if getattr(instance, "send_redis_signal_enabled", False):
            try:
                instance.send_redis_signal_enabled = False
            except Exception as exc:
                raise RuntimeError(
                    f"Backtest wrapper remained live at {location}: cannot disable send flag"
                ) from exc

        if hasattr(instance, "redis_client"):
            client = getattr(instance, "redis_client")
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
                try:
                    instance.redis_client = None
                except Exception as exc:
                    raise RuntimeError(
                        f"Backtest wrapper kept a Redis client at {location}: cannot clear redis_client"
                    ) from exc

        if getattr(instance, "send_redis_signal_enabled", False):
            raise RuntimeError(
                f"Backtest wrapper remained live at {location}: send_redis_signal_enabled=True"
            )
        if getattr(instance, "redis_client", None) is not None and not _is_inert_redis_client(
            getattr(instance, "redis_client")
        ):
            raise RuntimeError(
                f"Backtest wrapper kept a live Redis client at {location}: {type(instance.redis_client).__name__}"
            )


class StrategyLoader:
    def load(self, path: str | Path, state) -> LoadedStrategy:
        strategy_path = Path(path)
        with _runtime_state_scope(state):
            state.g = getattr(jqdata.runtime_state(), "g", state.g)
            exports = exported_globals()
            exports["g"] = state.g
            exports["log"] = state.log

            jqdata_module = ModuleType("jqdata")
            for name, value in exports.items():
                setattr(jqdata_module, name, value)

            spec = importlib.util.spec_from_file_location(strategy_path.stem, strategy_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load strategy file: {strategy_path}")
            module = importlib.util.module_from_spec(spec)
            module.__dict__.update(exports)
            module_slots = {
                "jqdata": jqdata_module,
                strategy_path.stem: module,
            }

            loaded_run_type = _runtime_mode(getattr(state, "context", None))
            backtest_mode = loaded_run_type == "backtest"
            safety = (
                BacktestRuntimeSafety(state, strategy_path, loaded_run_type=loaded_run_type)
                if backtest_mode
                else None
            )
            if safety is not None:
                safety.set_module_slots(module_slots)
            with _state_import_side_effect_guard(state):
                if safety is not None and hasattr(state.scheduler, "set_callback_wrapper"):
                    state.scheduler.set_callback_wrapper(safety.wrap_callback)
                elif hasattr(state.scheduler, "set_callback_wrapper"):
                    state.scheduler.set_callback_wrapper(
                        _strategy_callback_wrapper(
                            strategy_path,
                            module_slots,
                            state,
                            loaded_run_type,
                        )
                    )
                if safety is None:
                    with (
                        _temporary_module_slots(module_slots),
                        _strategy_import_path(strategy_path),
                        _pandas_legacy_panel_guard(),
                    ):
                        spec.loader.exec_module(module)
                else:
                    with (
                        _temporary_module_slots(module_slots),
                        _strategy_import_path(strategy_path),
                        _pandas_legacy_panel_guard(),
                        safety.redis_guard(),
                        safety.class_definition_guard(),
                    ):
                        spec.loader.exec_module(module)
                if safety is not None:
                    safety.patch_module(module)
        return LoadedStrategy(
            path=strategy_path,
            module=module,
            namespace=module.__dict__,
            module_slots=module_slots,
            state=state,
            loaded_run_type=loaded_run_type,
            safety=safety,
        )
