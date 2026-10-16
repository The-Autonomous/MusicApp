//! Calls the original native Tcl/Tk renderer directly. Python is not used by the UI.
use libloading::Library;
use std::{
    ffi::{c_char, c_int, c_void, CStr, CString},
    path::Path,
    sync::mpsc::Sender,
};
pub type Interp = c_void;
type Obj = c_void;
type GetString = unsafe extern "C" fn(*mut Obj) -> *const c_char;
type Eval = unsafe extern "C" fn(*mut Interp, *const c_char, c_int, c_int) -> c_int;
type ResultString = unsafe extern "C" fn(*mut Interp) -> *const c_char;
type Callback = unsafe extern "C" fn(*mut c_void, *mut Interp, c_int, *const *mut Obj) -> c_int;
struct Bridge {
    tx: Sender<Vec<String>>,
    get: GetString,
}
unsafe extern "C" fn action(
    data: *mut c_void,
    _interp: *mut Interp,
    n: c_int,
    args: *const *mut Obj,
) -> c_int {
    let bridge = &*(data as *const Bridge);
    let args = std::slice::from_raw_parts(args, n.max(0) as usize);
    let ascii = |index:usize| args.get(index).and_then(|obj| CStr::from_ptr((bridge.get)(*obj)).to_str().ok());
    let handle = |text:&str| if let Some(hex)=text.strip_prefix("0x"){isize::from_str_radix(hex,16).ok()}else{text.parse::<isize>().ok()};
    // Native placement/style actions carry only ASCII numbers. Borrow those
    // Tcl strings directly instead of allocating a Vec and several Strings.
    match ascii(1) {
        Some("overlay_style" | "search_style") => {
            if let Some(h)=ascii(2).and_then(handle).filter(|h|*h!=0){crate::platform::style_overlay(h,ascii(3)!=Some("0"));}
            return 0;
        },
        Some("overlay_place") => {
            if let (Some(h),Some(x),Some(y))=(ascii(2).and_then(handle).filter(|h|*h!=0),ascii(3).and_then(|s|s.parse::<i32>().ok()),ascii(4).and_then(|s|s.parse::<i32>().ok())){crate::platform::position(h,x,y);}
            return 0;
        },
        _ => {}
    }
    let mut values = Vec::with_capacity(args.len().saturating_sub(1));
    for &obj in args
        .iter()
        .skip(1)
    {
        let bytes = CStr::from_ptr((bridge.get)(obj)).to_bytes();
        values.push(
            cesu8::from_java_cesu8(bytes)
                .map(|v| v.into_owned())
                .unwrap_or_else(|_| String::from_utf8_lossy(bytes).into_owned()),
        );
    }
    let _ = bridge.tx.send(values);
    0
}
pub fn word(value: &str) -> String {
    let mut out=String::with_capacity(value.len()+2);
    append_word(&mut out,value);
    out
}
fn append_word(out:&mut String,value:&str) {
    // Tcl substitution is disabled for every data value. Never evaluate library titles as code.
    if value.is_empty() {
        out.push_str("{}"); return;
    }
    for c in value.chars() {
        match c {
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            ' ' | ';' | '[' | ']' | '{' | '}' | '$' | '"' | '\\' => {
                out.push('\\');
                out.push(c)
            }
            '\0' => out.push_str("\\u0000"),
            _ => out.push(c),
        }
    }
}
pub struct Tk {
    interp: *mut Interp,
    eval: Eval,
    result: ResultString,
    event: unsafe extern "C" fn(c_int) -> c_int,
    delete: unsafe extern "C" fn(*mut Interp),
    bridge: *mut Bridge,
    // Tk registers Win32 window procedures and thread finalizers. Those can be
    // called after the interpreter is deleted; keep the modules mapped until
    // the OS ends this process rather than unloading live callback addresses.
    _tk: std::mem::ManuallyDrop<Library>,
    _tcl: std::mem::ManuallyDrop<Library>,
}
impl Tk {
    pub fn new(base: &Path, tx: Sender<Vec<String>>) -> Result<Self, String> {
        unsafe {
            let runtime = base.join("runtime");
            let tcl_dir = runtime.join("tcl8.6");
            let tk_dir = runtime.join("tk8.6");
            std::env::set_var("TCL_LIBRARY", &tcl_dir);
            std::env::set_var("TK_LIBRARY", &tk_dir);
            let tcl = Library::new(runtime.join("tcl86t.dll"))
                .map_err(|e| format!("Native Tcl runtime missing: {e}. Run build.bat."))?;
            let tk = Library::new(runtime.join("tk86t.dll")).map_err(|e| e.to_string())?;
            let find: libloading::Symbol<unsafe extern "C" fn(*const c_char)> = tcl
                .get(b"Tcl_FindExecutable\0")
                .map_err(|e| e.to_string())?;
            let executable = CString::new(
                std::env::current_exe()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .as_bytes(),
            )
            .unwrap();
            find(executable.as_ptr());
            let create: libloading::Symbol<unsafe extern "C" fn() -> *mut Interp> =
                tcl.get(b"Tcl_CreateInterp\0").map_err(|e| e.to_string())?;
            let interp = create();
            let init: libloading::Symbol<unsafe extern "C" fn(*mut Interp) -> c_int> =
                tcl.get(b"Tcl_Init\0").map_err(|e| e.to_string())?;
            let result: ResultString = *tcl
                .get(b"Tcl_GetStringResult\0")
                .map_err(|e| e.to_string())?;
            if init(interp) != 0 {
                return Err(CStr::from_ptr(result(interp)).to_string_lossy().into());
            }
            let tk_init: libloading::Symbol<unsafe extern "C" fn(*mut Interp) -> c_int> =
                tk.get(b"Tk_Init\0").map_err(|e| e.to_string())?;
            if tk_init(interp) != 0 {
                return Err(CStr::from_ptr(result(interp)).to_string_lossy().into());
            }
            let get: GetString = *tcl.get(b"Tcl_GetString\0").map_err(|e| e.to_string())?;
            let register: libloading::Symbol<
                unsafe extern "C" fn(
                    *mut Interp,
                    *const c_char,
                    Callback,
                    *mut c_void,
                    Option<unsafe extern "C" fn(*mut c_void)>,
                ) -> *mut c_void,
            > = tcl
                .get(b"Tcl_CreateObjCommand\0")
                .map_err(|e| e.to_string())?;
            let bridge = Box::into_raw(Box::new(Bridge { tx, get }));
            register(
                interp,
                b"rust_action\0".as_ptr().cast(),
                action,
                bridge.cast(),
                None,
            );
            let instance = Self {
                interp,
                eval: *tcl.get(b"Tcl_EvalEx\0").map_err(|e| e.to_string())?,
                result,
                event: *tcl.get(b"Tcl_DoOneEvent\0").map_err(|e| e.to_string())?,
                delete: *tcl.get(b"Tcl_DeleteInterp\0").map_err(|e| e.to_string())?,
                bridge,
                _tk: std::mem::ManuallyDrop::new(tk),
                _tcl: std::mem::ManuallyDrop::new(tcl),
            };
            instance
                .eval("wm withdraw .; proc bgerror {message} {rust_action ui_error $message}")?;
            Ok(instance)
        }
    }
    pub fn eval(&self, code: &str) -> Result<String, String> {
        unsafe {
            let bytes = cesu8::to_java_cesu8(code);
            // EvalEx accepts an explicit byte count; a second NUL-terminated
            // allocation and copy is unnecessary for every UI state update.
            let length=c_int::try_from(bytes.len()).map_err(|_|"Tcl script exceeds supported size".to_string())?;
            let status = (self.eval)(self.interp, bytes.as_ptr().cast(), length, 0);
            let bytes = CStr::from_ptr((self.result)(self.interp)).to_bytes();
            let value = cesu8::from_java_cesu8(bytes)
                .map(|s| s.into_owned())
                .unwrap_or_else(|_| String::from_utf8_lossy(bytes).into());
            if status == 0 {
                Ok(value)
            } else {
                Err(format!("Tcl: {value}"))
            }
        }
    }
    pub fn call(&self, name: &str, args: &[&str]) -> Result<String, String> {
        let mut code=String::with_capacity(name.len()+args.iter().map(|s|s.len()+2).sum::<usize>());
        code.push_str(name);
        for arg in args {code.push(' ');append_word(&mut code,arg);}
        self.eval(&code)
    }
    pub fn event(&self) {
        unsafe {
            (self.event)(0);
        }
    }
}
impl Drop for Tk {
    fn drop(&mut self) {
        unsafe {
            (self.delete)(self.interp);
            drop(Box::from_raw(self.bridge));
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn data_is_quoted() {
        assert_eq!(word("$x;[exit]"), "\\$x\\;\\[exit\\]");
        assert_eq!(word("a\nb"), "a\\nb");
    }
    #[test]
    fn native_cards_unicode_and_data_never_execute_as_code() {
        let (tx, _rx) = std::sync::mpsc::channel();
        let base = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let ui = Tk::new(base, tx).expect("native Tcl/Tk runtime");
        ui.eval(&format!(
            "source -encoding utf-8 {}",
            word(&base.join("ui/app.tcl").to_string_lossy())
        ))
        .unwrap();
        let title = "Artist · 漢字 🎵 $x;[set ::injected 1]";
        assert_eq!(ui.eval("::music::show_overlay; winfo exists .overlay").unwrap(), "0");
        ui.eval("::music::overlay_available 1").unwrap();
        assert_eq!(ui.eval(".overlay.c itemcget hint -state").unwrap(), "hidden");
        ui.eval("::music::overlay_drag_begin").unwrap();
        assert_eq!(ui.eval("expr {$::music::drag_pulse_timer ne {}}").unwrap(), "1");
        assert_eq!(ui.eval(".overlay.c itemcget hint -state").unwrap(), "normal");
        ui.eval("::music::overlay_drag_end").unwrap();
        assert_eq!(ui.eval("set ::music::drag_pulse_timer").unwrap(), "");
        assert!(ui.eval("after info $::music::hint_hide_timer").unwrap().contains("hint_fade 0.0"));
        ui.eval("::music::hide_overlay").unwrap();
        assert_eq!(ui.eval("set ::music::hint_hide_timer").unwrap(), "");
        assert_eq!(ui.eval(".overlay.c itemcget hint -state").unwrap(), "hidden");
        ui.call("::music::set_display", &[title, "12", "180", "Line one"])
            .unwrap();
        ui.call("::music::show_overlay", &[]).unwrap();
        assert_eq!(ui.eval("set ::music::title").unwrap(), title);
        assert_eq!(ui.eval("info exists ::injected").unwrap(), "0");
        assert_eq!(ui.eval("wm attributes .overlay -alpha").unwrap(), "0.7");
        assert_eq!(
            ui.eval("wm attributes .overlay -transparentcolor").unwrap(),
            "#ff00ff"
        );
        for command in [
            "::music::controls",
            "destroy .controls",
            "::music::controls 1",
            "destroy .controls",
            "::music::search",
            "::music::close_search",
            "::music::eq",
            "destroy .eq",
            "::music::youtube",
            "::music::youtube_state downloading {Downloading your music} 1 0 1 2 10",
            "::music::youtube_collapse",
            "::music::youtube_restore",
            "::music::youtube_options",
            "update idletasks",
        ] {
            ui.eval(command)
                .unwrap_or_else(|e| panic!("{command}: {e}"));
        }
        ui.eval("::music::show_overlay; ::music::controls; update idletasks; ::music::authentication_ui 1").unwrap();
        assert_eq!(ui.eval("wm state .overlay").unwrap(), "withdrawn");
        assert_eq!(ui.eval("wm state .controls").unwrap(), "withdrawn");
        ui.eval("::music::eq; update idletasks").unwrap();
        assert_eq!(ui.eval("wm state .eq").unwrap(), "withdrawn");
        ui.eval("::music::authentication_ui 0; update idletasks").unwrap();
        assert_eq!(ui.eval("wm state .overlay").unwrap(), "normal");
        assert_eq!(ui.eval("wm state .controls").unwrap(), "normal");
        ui.eval("destroy .").unwrap();
    }
}
