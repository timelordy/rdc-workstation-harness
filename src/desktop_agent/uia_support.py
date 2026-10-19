"""UIA state helpers with no Windows imports, for portable behaviour tests.

The caller owns the control's COM apartment. These helpers return plain data
only; they never retain a UIA wrapper or an interface beyond the call.
"""

import math


MAX_TEXT = 20000
MAX_WAIT = 120.0
WAIT_STATES = {"exists", "visible", "enabled", "ready", "active"}
NEGATIVE_STATES = {
    "absent": "exists", "not_exists": "exists",
    "hidden": "visible", "disabled": "enabled", "inactive": "active",
}

# Include old CamelCase aliases after removing underscores. UIA scroll/select
# remain semantic actions; the mouse/keyboard input methods require opt-in.
PHYSICAL_METHODS = {
    "clickinput", "doubleclickinput", "rightclickinput",
    "pressmouseinput", "releasemouseinput", "movemouseinput",
    "dragmouseinput", "wheelmouseinput", "typekeys",
}


def is_physical_method(name):
    normalized = name.replace("_", "").lower()
    return normalized in PHYSICAL_METHODS or name.lower().endswith("_input")


def wait_options(request):
    state = request.get("state", "exists enabled visible ready")
    if not isinstance(state, str) or not state.strip():
        raise ValueError("state must be a non-empty string")
    state = " ".join(state.lower().split())
    negative = state in NEGATIVE_STATES
    if not negative and any(part not in WAIT_STATES for part in state.split()):
        raise ValueError(
            "state must contain exists, visible, enabled, ready or active; "
            "or be absent, not_exists, hidden, disabled or inactive"
        )
    timeout = _finite_number(request.get("timeout", 10), "timeout", 0, MAX_WAIT)
    retry = _finite_number(request.get("retry_interval", 0.2), "retry_interval", 0.01, 2)
    return state, NEGATIVE_STATES.get(state, state), negative, timeout, retry


def _finite_number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number from {minimum} to {maximum}")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name} must be a finite number from {minimum} to {maximum}")
    return number


def text_limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TEXT:
        raise ValueError(f"max_text must be an integer from 0 to {MAX_TEXT}")
    return value


def wait_for_spec(spec, request):
    """Wait on the unresolved specification, including controls not yet there.

    Return plain condition metadata. The caller may resolve a wrapper after a
    positive wait; a negative wait never tries to resolve an absent control.
    """
    state, condition, negative, timeout, retry = wait_options(request)
    method = spec.wait_not if negative else spec.wait
    method(condition, timeout=timeout, retry_interval=retry)
    result = {"ok": True, "state": state}
    if condition == "exists" or not negative:
        result["exists"] = not negative
    return result


def password_state(control):
    """Do not read Value/Text until the provider confirms this is not a password.

    CurrentIsPassword is the documented UIA property. An unavailable provider
    yields unknown, which also causes value/text redaction.
    """
    try:
        return bool(control.element_info.element.CurrentIsPassword), None
    except Exception as error:
        return None, {"type": type(error).__name__, "message": "password state unavailable"}


def _error(error, message="provider state unavailable"):
    # Provider errors can embed control contents: expose the type, not raw text.
    return {"type": type(error).__name__, "message": message}


def _interface(control, attribute):
    try:
        interface = getattr(control, attribute)
        if interface is None:
            return None, {"supported": False, "error": {"message": "pattern not supported"}}
        return interface, {"supported": True}
    except Exception as error:
        absent = isinstance(error, AttributeError) or type(error).__name__ == "NoPatternInterfaceError"
        return None, {
            "supported": False if absent else None,
            "error": _error(error, "pattern not supported" if absent else "pattern availability unknown"),
        }


def _field(result, name, read):
    try:
        result[name] = read()
    except Exception as error:
        result.setdefault("errors", {})[name] = _error(error)


def _bounded_text(result, name, read, limit):
    if limit == 0:
        result[name + "_omitted"] = True
        return
    try:
        value = str(read() or "")
        result[name] = value[:limit]
        result[name + "_truncated"] = len(value) > limit
    except Exception as error:
        result.setdefault("errors", {})[name] = _error(error)


def inspect_control(control, describe_control, max_text=4000):
    """Read supported semantic patterns without invoking or changing the control."""
    max_text = text_limit(max_text)
    password, password_error = password_state(control)
    redact = password is not False
    # Accessible Name avoids pywinauto.window_text()/rich_text pulling an entire
    # document a second time while constructing ordinary control metadata.
    metadata = describe_control(control, redact_text=redact, use_name=True)
    if not redact and isinstance(metadata.get("title"), str):
        metadata["title_truncated"] = len(metadata["title"]) > max_text
        metadata["title"] = metadata["title"][:max_text]
    result = {
        "control": metadata,
        "password": password,
        "patterns": {},
    }
    if password_error:
        result["password_error"] = password_error
    patterns = result["patterns"]
    names = {
        "value": "iface_value", "toggle": "iface_toggle",
        "selection_item": "iface_selection_item",
        "expand_collapse": "iface_expand_collapse",
        "range_value": "iface_range_value", "text": "iface_text",
    }
    for name, attribute in names.items():
        interface, info = _interface(control, attribute)
        patterns[name] = info
        if interface is None:
            continue
        if name in {"value", "text", "range_value"} and redact:
            info.update({"redacted": True, "reason": "password" if password else "password_state_unavailable"})
            continue
        if name == "value":
            _field(info, "read_only", lambda: bool(interface.CurrentIsReadOnly))
            _bounded_text(info, "value", lambda: interface.CurrentValue, max_text)
        elif name == "toggle":
            _field(info, "state", lambda: int(interface.CurrentToggleState))
            info["state_name"] = {0: "off", 1: "on", 2: "indeterminate"}.get(info.get("state"), "unknown")
        elif name == "selection_item":
            _field(info, "selected", lambda: bool(interface.CurrentIsSelected))
        elif name == "expand_collapse":
            _field(info, "state", lambda: int(interface.CurrentExpandCollapseState))
            info["state_name"] = {
                0: "collapsed", 1: "expanded", 2: "partially_expanded", 3: "leaf_node",
            }.get(info.get("state"), "unknown")
        elif name == "range_value":
            for field, property_name in (
                ("value", "CurrentValue"), ("minimum", "CurrentMinimum"),
                ("maximum", "CurrentMaximum"), ("small_change", "CurrentSmallChange"),
                ("large_change", "CurrentLargeChange"),
            ):
                _field(info, field, lambda attr=property_name: float(getattr(interface, attr)))
            _field(info, "read_only", lambda: bool(interface.CurrentIsReadOnly))
        elif name == "text":
            # Bound the provider's retrieval too, rather than reading the entire
            # document and slicing it afterward. One extra character flags truncation.
            _bounded_text(info, "text", lambda: interface.DocumentRange.GetText(max_text + 1), max_text)
    return result
