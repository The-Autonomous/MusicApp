"""Sanitized benchmark settings only; never reads account or cookie files."""
import json
import math

CONTROLLED_SCENARIOS = ["idle_visible", "idle_hidden", "paused_visible", "paused_hidden", "playback_bypass", "playback_eq", "playback_echo", "controls", "search", "progress"]
SCENARIOS = CONTROLLED_SCENARIOS + ["playback_profile"]
ISO_BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
CONTROLLED_GAINS = [3, -2, 6, -7, 2, -3, 10, -4, 5, 8]
PROFILE_FIELDS = {"volume": (int, float), "gaming_mode": bool, "auto_gaming": bool,
                  "accept_radio_eq": bool, "youtube_download_permanently": bool,
                  "do_youtube_search": bool, "lyrics_visible": bool}


def sanitized_profile(directory):
    """Whitelist settings; never enumerate/copy account, cookie or history files."""
    if directory is None: return {"preferences": {}, "eq_gains": [0.] * 10, "bindings": {}}
    directory = directory.resolve()
    preference_path = directory / "preferences.json"
    if not preference_path.is_file(): raise ValueError(f"Imported preferences are missing: {preference_path}")
    raw = json.loads(preference_path.read_text(encoding="utf-8-sig"))
    preferences = {key: raw[key] for key, kind in PROFILE_FIELDS.items() if key in raw and isinstance(raw[key], kind)}
    if "volume" in preferences:
        if not math.isfinite(preferences["volume"]): raise ValueError("Profile volume is not finite")
        preferences["volume"] = min(1., max(0., preferences["volume"]))
    gains_path = directory / "musicapp_eq.json"
    # The actual Python source uses this NON-DOT filename. The similarly named
    # .musicapp_eq.json is not read by its audio_eq.py implementation.
    raw_gains = json.loads(gains_path.read_text(encoding="utf-8-sig")) if gains_path.is_file() else {}
    gains = []
    for band in ISO_BANDS:
        value = float(raw_gains.get(str(band), 0.))
        if not math.isfinite(value): raise ValueError(f"Profile EQ gain is not finite: {band}")
        gains.append(min(12., max(-12., value)))
    bindings_path = directory / ".keyBindings.json"
    bindings = {}
    if bindings_path.is_file():
        raw_bindings = json.loads(bindings_path.read_text(encoding="utf-8-sig"))
        allowed = {"skip_previous", "skip_next", "volume_down", "volume_up", "pause_play", "repeat_toggle", "lyrics_toggle_visibility", "radio_enable_toggle", "radio_scan_station", "show_search", "show_eq_menu", "youtube_sync", "toggle_overlay"}
        for action in allowed:
            value = raw_bindings.get(action)
            if isinstance(value, dict) and isinstance(value.get("required"), list) and all(isinstance(key, str) and len(key) < 50 for key in value["required"]):
                bindings[action] = {"required": value["required"], "alt_needed": bool(value.get("alt_needed", True))}
    return {"preferences": preferences, "eq_gains": gains, "bindings": bindings}


def prepare_trial_profile(state, scenario, profile):
    preferences = dict(profile["preferences"])
    preferences.update(path=None, elapsed=0., paused=False, repeat=False, player_enabled=True,
                       visible=not scenario.endswith("hidden"), position=None, host_radio=False)
    gains = list(profile["eq_gains"])
    if scenario != "playback_profile":
        preferences["gaming_mode"] = scenario not in ("playback_eq", "playback_echo")
        gains = CONTROLLED_GAINS[:] if scenario == "playback_eq" else [0.] * 10
    expected = {"gaming_mode": bool(preferences.get("gaming_mode", True)), "eq_gains": gains,
                "echo": {"delay_ms": 350., "feedback": .35, "wet": .5} if scenario == "playback_echo" else None,
                "volume": 0., "nominal_profile_volume": profile["preferences"].get("volume", .5)}
    (state / "preferences.json").write_text(json.dumps(preferences), encoding="utf-8")
    (state / "musicapp_eq.json").write_text(json.dumps(dict(zip(map(str, ISO_BANDS), gains))), encoding="utf-8")
    (state / ".keyBindings.json").write_text(json.dumps(profile["bindings"]), encoding="utf-8")
    (state / "benchmark_expected_audio.json").write_text(json.dumps(expected), encoding="utf-8")
    return expected


def validate_audio(diagnostic, expected, scenario, seconds):
    if diagnostic.get("last_error") or diagnostic.get("duration_seconds", 0) <= 0:
        raise RuntimeError(f"Audio validation failed: {diagnostic}")
    if scenario.startswith("playback_") and diagnostic.get("position_seconds", 0) < max(.05, seconds * .5):
        raise RuntimeError(f"Audio clock did not advance: {diagnostic}")
    if scenario.startswith("paused_") and not diagnostic.get("is_paused"):
        raise RuntimeError(f"Paused workload was not paused: {diagnostic}")
    if diagnostic.get("gaming_mode") != expected["gaming_mode"]:
        raise RuntimeError(f"Gaming mode differs from prescribed workload: {diagnostic}")
    actual = diagnostic.get("eq_gains", [])
    if len(actual) != 10 or any(abs(a-b) > 1e-5 for a, b in zip(actual, expected["eq_gains"])):
        raise RuntimeError(f"EQ differs from prescribed workload: expected={expected['eq_gains']}, actual={actual}")
    if expected["echo"] is None:
        if diagnostic.get("echo") is not None: raise RuntimeError("Unexpected echo in workload")
    else:
        echo = diagnostic.get("echo")
        if not isinstance(echo, dict) or any(abs(echo.get(key, -1)-value) > 1e-5 for key, value in expected["echo"].items()):
            raise RuntimeError(f"Echo differs from prescribed workload: {echo}")
    if abs(diagnostic.get("volume", 1)) > 1e-6: raise RuntimeError("Muted benchmark output was not muted")


