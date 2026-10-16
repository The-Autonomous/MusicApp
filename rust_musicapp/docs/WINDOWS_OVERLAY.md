# Windows overlay and input design

The Rust hook thread uses WH_KEYBOARD_LL and WH_MOUSE_LL, the same Windows hook
classes used by the source's pynput implementation. Callbacks enqueue events
and immediately call the next hook; they do not perform network, drawing or DSP
work. The installing thread maintains a Windows message loop. Left/right Alt
and Shift remain distinct, and normal input continues to the foreground app.

The passive overlay uses WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE and
WS_EX_TOOLWINDOW. HWND_TOPMOST repairs use SWP_NOACTIVATE. Right-drag updates the
owned window directly without injecting clicks or moving keyboard focus away
from a game. Position checks use native monitor work areas, including negative
desktop coordinates. Interactive cards accept focus separately.

Native drag movement is coalesced before the UI consumes it. Monitor geometry
is cached per gesture, and position-only moves avoid Tk geometry flushes. The
event loop wakes every 8 ms while dragging and returns to its regular idle
cadence on release. The controls hint fades in during right dragging and fades
out ten seconds after release; hidden windows cancel its timers.

The remembered visibility preference is separate from startup/authentication
gating. A loaded paused track is still eligible for display. Interactive browser
authentication withdraws all MusicApp windows before Chrome/Edge launches,
maximizes the browser normally, and restores surviving prior windows afterward.

The native renderer modules stay mapped for process lifetime because Windows
can invoke their registered window procedures and thread finalizers after a Tcl
interpreter is deleted. This avoids unloading callback addresses during teardown.

These choices preserve ordinary desktop/borderless-window overlay behavior.
They are not an assurance of priority above exclusive-fullscreen presentation,
secure desktops, anti-cheat input filters or all elevated topmost windows.
UIAccess is a separate Windows deployment privilege requiring signed binaries
and a protected installation location. This local port does not change security
policy or install itself outside the project to obtain that privilege.

Primary references consulted:

- [LowLevelKeyboardProc and hook timeouts](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc)
- [SetWindowPos and topmost behavior](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowpos)
- [Extended window styles](https://learn.microsoft.com/en-us/windows/win32/winmsg/extended-window-styles)
- [UIAccess security and deployment requirements](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-securityoverview)
- [Microsoft topmost utility limitations](https://learn.microsoft.com/en-us/windows/powertoys/always-on-top)
