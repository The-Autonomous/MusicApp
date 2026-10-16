# Native presentation API

`ui/app.tcl` runs directly in the embedded Tcl/Tk 8.6 interpreter. It uses the
same engine, fonts, theme, polygon construction and metric based layout as the
original Python interface. It starts no Python process and imports no personal
settings. Source it after registering `rust_action`, explicitly using
`source -encoding utf-8 {path/to/app.tcl}`. Windows Tcl otherwise decodes source
with its system code page, corrupting Unicode labels and their measurements.

All calls and widget operations must run on the interpreter's owner thread. Pass
arguments through Tcl object/list APIs or proper Tcl list quoting, never string
concatenation of titles, paths, lyrics or search text. Text can contain brackets,
semicolons, backslashes and Unicode. Window handles are returned as Tk integer
strings (typically hexadecimal); resolve the native parent for Windows styles.

## Rust calls into Tcl

| Procedure in `::music` | Arguments / behavior |
|---|---|
| `show_overlay`, `hide_overlay` | Show or withdraw `.overlay`; showing requires playback eligibility and no authentication. `.overlay.c` is its canvas. |
| `overlay_available` | `bool`; current readiness/audio/user visibility gate. Search temporarily suppresses eligible playback. |
| `authentication_ui` | `bool`; withdraw/restore MusicApp windows around browser authentication. |
| `overlay_drag_begin`, `overlay_drag_end` | Start/stop transient drag wake timer and controls hint; fade out ten seconds after release. |
| `set_position` | `x y`; native parent should clamp across monitor work areas. |
| `set_display` | `title elapsed_seconds total_seconds lyrics`; lays out only when text/width changes. |
| `controls` | optional `customize=0`; toggle `.controls`. |
| `binding_update` | `action_id chord_list require_left_alt ?status_message?`; refresh shortcut UI. |
| `search` | Toggle `.search`; hides and later restores player. |
| `search_submit` | optional `term dont_log=0`; use for initial recommendation. |
| `results` | One Tcl list of `{display_title path_or_url type}` rows. Types: `path`, `url`, `playlist`, `title_only`. |
| `close_search` | Closes after playback/download; checks current playback eligibility before restoring the player. |
| `eq` | Toggle `.eq`, then requests current DSP state. |
| `eq_state` | `gains_list volume_percent delay_ms wet_percent gaming_bool radio_eq_bool`; 10 ISO gains in order. |
| `youtube` | optional `startup=0`; toggle `.youtube`, preserving ongoing sync. |
| `youtube_state` | `phase message connected ready busy saved total ?remembered=1?`; `total` empty means unknown. |
| `youtube_downloads` | One Tcl list of `{display_text fraction}` rows; at most three rendered; empty fraction indeterminate. |
| `youtube_collapse`, `youtube_restore` | Loading helper minimization never starts/cancels sync or allows playback. |
| `youtube_close` | Startup helper minimizes until ready; otherwise closes. |
| `youtube_options` | Show `.youtube_options`. |
| `options_state` | `source_name hosting_bool auto_gaming_bool`. |

The root `.` stays withdrawn. To set lyric visibility, set
`::music::lyrics_visible` and call `::music::draw_overlay`. Search preferences are
`::music::use_youtube` and `::music::permanent`; both initialize to source defaults.
`::music::status` can display key binding conflicts. `::music::edit_action` tracks
the shortcut awaiting capture. Rust should clear it on successful capture/cancel.

The overlay default is 0.7 alpha, black background, #ff00ff transparency key,
15px corner radius and padding. Segoe UI fonts: title 14 bold, time 12,
lyrics 11 italic, hint 9. `show_overlay` emits native style application after
window creation; Rust must implement click-through/NOACTIVATE and topmost repair.
Rust's global mouse hook should handle right-drag without giving a game focus
away. Source behavior needs native multi-monitor clamp and persistence.

## Tcl calls into Rust

Every user action invokes `rust_action action ?args...?`; enqueue work and return
promptly. Do not synchronously reenter UI functions inside the callback.

| Action | Arguments |
|---|---|
| Source action IDs | `skip_previous`, `skip_next`, `pause_play`, `volume_down`, `volume_up`, `repeat_toggle`, `lyrics_toggle_visibility`, `radio_enable_toggle`, `radio_scan_station`, `show_search`, `show_eq_menu`, `youtube_sync`, `toggle_overlay`, `player_on_off`, `show_hints`, `player_restart`, `kill_self`; no args. |
| `overlay_style` | `winfo_id clickthrough=1`; also needs NOACTIVATE. |
| `search_style` | `winfo_id clickthrough=1`; downloading record animation. |
| `overlay_position` | `x y`; right-drag ended. |
| `binding_edit` | `action_id`; capture next distinguishing key from global hook, preserving Alt side. |
| `binding_cancel`, `binding_reset` | no args. |
| `binding_alt` | `action_id`; checkbox has already changed `::music::alt_required(action_id)`. Validate collisions before saving, and publish `binding_update`. |
| `search_recommendation` | no args; compute recommendation and optionally call `search_submit term 1`. |
| `search` | `term use_youtube dont_log`. |
| `search_youtube` | `bool`; preference changed. |
| `download_permanent` | `bool`. |
| `search_select` | `path_or_url type`. |
| `eq_request` | no args; respond with `eq_state`. |
| `eq_band` | `frequency_hz gain_db`. |
| `eq_preset` | `name gain0 ... gain9`; exact source preset arrays included. |
| `echo_delay`, `echo_wet` | `delay_ms` or `wet_percent`; wet zero disables echo. |
| `volume` | `percent`; source slider range 0..100. |
| `gaming`, `accept_radio_eq` | `bool`. |
| `youtube_start`, `youtube_cancel`, `youtube_forget`, `sync_playlists` | no args. |
| `options_request` | no args; respond with `options_state`. |
| `options_save` | `source_name hosting_bool auto_gaming_bool`. |

## Verification

`python -B benchmarks/verify_ui.py` uses Python only as a development test harness for the
same Tcl interpreter. It exercises all cards, dynamic progress, search selection,
shortcuts and DSP controls. It also compares the original Python and Tcl control
layout at Windows text scaling factors 1.3333, 1.6667 and 2.0, and verifies overlay
canvas metrics against `GhostOverlay.update_display` with synthetic content.
No player, audio device, account, input hook or original settings file is started.

The UI preserves existing behavior including the source EQ card's tight layout.
Exclusive fullscreen, elevated windows and protected desktops are native Windows
constraints; ordinary Tk topmost is not an assurance against those cases.
