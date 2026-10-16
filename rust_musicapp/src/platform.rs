//! Windows integration: same low-level hook class as pynput; callbacks only enqueue.
use std::{
    cell::RefCell,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::Sender,
        Arc,
    },
    thread,
    time::Duration,
};
#[repr(C)]
#[derive(Default, Clone, Copy, Debug)]
pub struct Point {
    pub x: i32,
    pub y: i32,
}
#[repr(C)]
#[derive(Default, Clone, Copy, Debug)]
pub struct Rect {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}
#[repr(C)]
struct Msg {
    hwnd: isize,
    message: u32,
    wparam: usize,
    lparam: isize,
    time: u32,
    pt: Point,
    private: u32,
}
#[repr(C)]
struct Key {
    vk: u32,
    scan: u32,
    flags: u32,
    time: u32,
    extra: usize,
}
#[repr(C)]
struct Mouse {
    pt: Point,
    data: u32,
    flags: u32,
    time: u32,
    extra: usize,
}
#[repr(C)]
struct MonitorInfo {
    size: u32,
    monitor: Rect,
    work: Rect,
    flags: u32,
}
type HookProc = unsafe extern "system" fn(i32, usize, isize) -> isize;
#[link(name = "user32")]
extern "system" {
    fn SetWindowsHookExW(id: i32, callback: Option<HookProc>, module: isize, thread: u32) -> isize;
    fn CallNextHookEx(hook: isize, code: i32, wp: usize, lp: isize) -> isize;
    fn UnhookWindowsHookEx(hook: isize) -> i32;
    fn GetMessageW(msg: *mut Msg, hwnd: isize, min: u32, max: u32) -> i32;
    fn TranslateMessage(msg: *const Msg) -> i32;
    fn DispatchMessageW(msg: *const Msg) -> isize;
    fn PostThreadMessageW(thread: u32, msg: u32, wp: usize, lp: isize) -> i32;
    fn GetParent(hwnd: isize) -> isize;
    fn GetWindowLongPtrW(hwnd: isize, index: i32) -> isize;
    fn SetWindowLongPtrW(hwnd: isize, index: i32, value: isize) -> isize;
    fn SetWindowPos(hwnd: isize, after: isize, x: i32, y: i32, cx: i32, cy: i32, flags: u32)
        -> i32;
    fn GetWindowRect(hwnd: isize, rect: *mut Rect) -> i32;
    fn EnumDisplayMonitors(
        dc: isize,
        clip: *const Rect,
        callback: Option<unsafe extern "system" fn(isize, isize, *mut Rect, isize) -> i32>,
        data: isize,
    ) -> i32;
    fn GetMonitorInfoW(monitor: isize, info: *mut MonitorInfo) -> i32;
    fn GetAsyncKeyState(key: i32) -> i16;
    fn MessageBoxW(hwnd: isize, text: *const u16, title: *const u16, flags: u32) -> i32;
    fn GetLastInputInfo(info: *mut LastInput) -> i32;
    fn SetProcessDPIAware() -> i32;
    fn SetTimer(
        hwnd: isize,
        id: usize,
        interval: u32,
        callback: Option<unsafe extern "system" fn(isize, u32, usize, u32)>,
    ) -> usize;
    fn KillTimer(hwnd: isize, id: usize) -> i32;
}
#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentThreadId() -> u32;
    fn GetTickCount() -> u32;
}
#[repr(C)]
struct LastInput {
    size: u32,
    tick: u32,
}
thread_local! {static EVENTS:RefCell<Option<Sender<Vec<String>>>>=const{RefCell::new(None)}; static RIGHT:RefCell<bool>=const{RefCell::new(false)};}
static DRAG_TRACKING: AtomicBool = AtomicBool::new(false);
pub fn set_drag_tracking(active: bool) {
    DRAG_TRACKING.store(active, Ordering::Relaxed);
}
fn send(event: Vec<String>) {
    EVENTS.with(|tx| {
        if let Some(tx) = tx.borrow().as_ref() {
            let _ = tx.send(event);
        }
    });
}
pub fn key_name(vk: u32, scan: u32, flags: u32) -> String {
    match vk {
        0xa4 => "alt".into(),
        0xa5 => "right alt".into(),
        0x12 => if flags & 1 != 0 { "right alt" } else { "alt" }.into(),
        0xa0 => "shift".into(),
        0xa1 => "right shift".into(),
        0x10 => if scan == 0x36 { "right shift" } else { "shift" }.into(),
        0xa2 | 0xa3 | 0x11 => "ctrl".into(),
        0x1b => "escape".into(),
        0x20 => "space".into(),
        0x0d => "enter".into(),
        0x09 => "tab".into(),
        0x24 => "home".into(),
        0x23 => "end".into(),
        0x21 => "page_up".into(),
        0x22 => "page_down".into(),
        0x2d => "insert".into(),
        0x08 => "backspace".into(),
        0x2e => "delete".into(),
        0x25 => "left".into(),
        0x26 => "up".into(),
        0x27 => "right".into(),
        0x28 => "down".into(),
        0xbd => "-".into(),
        0xbb => "=".into(),
        0xdb => "[".into(),
        0xdd => "]".into(),
        0xba => ";".into(),
        0xde => "'".into(),
        0xbf => "/".into(),
        0xdc => "\\".into(),
        0xc0 => "`".into(),
        0xbe => ".".into(),
        0xbc => ",".into(),
        0xb0 => "media_next".into(),
        0xb1 => "media_previous".into(),
        0xb3 => "media_play_pause".into(),
        0x30..=0x39 | 0x41..=0x5a => char::from_u32(vk).unwrap().to_ascii_lowercase().to_string(),
        0x60..=0x69 => format!("<{}>", vk - 0x60),
        0x70..=0x87 => format!("f{}", vk - 0x6f),
        _ => String::new(),
    }
}
unsafe extern "system" fn keyboard(code: i32, wp: usize, lp: isize) -> isize {
    if code >= 0 && matches!(wp, 0x100 | 0x104 | 0x101 | 0x105) {
        let k = &*(lp as *const Key);
        let name = key_name(k.vk, k.scan, k.flags);
        if !name.is_empty() {
            send(vec![
                "key".into(),
                if matches!(wp, 0x100 | 0x104) {
                    "down"
                } else {
                    "up"
                }
                .into(),
                name,
            ]);
        }
    }
    CallNextHookEx(0, code, wp, lp)
}
unsafe extern "system" fn mouse(code: i32, wp: usize, lp: isize) -> isize {
    if code >= 0 && matches!(wp, 0x204 | 0x205 | 0x200) {
        let emit = RIGHT.with(|down| {
            if wp == 0x204 {
                *down.borrow_mut() = true;
            }
            let emit = *down.borrow();
            if wp == 0x205 {
                *down.borrow_mut() = false;
            }
            emit && (wp != 0x200 || DRAG_TRACKING.load(Ordering::Relaxed))
        });
        if wp == 0x205 {
            set_drag_tracking(false);
        }
        if emit {
            let m = &*(lp as *const Mouse);
            send(vec![
                "mouse".into(),
                match wp {
                    0x204 => "down",
                    0x205 => "up",
                    _ => "move",
                }
                .into(),
                m.pt.x.to_string(),
                m.pt.y.to_string(),
            ]);
        }
    }
    CallNextHookEx(0, code, wp, lp)
}
pub struct Hooks {
    thread_id: u32,
    join: Option<thread::JoinHandle<()>>,
}
impl Hooks {
    pub fn start(tx: Sender<Vec<String>>) -> Result<Self, String> {
        let (ready_rx_tx, ready) = std::sync::mpsc::channel::<Result<u32, String>>();
        let join = thread::spawn(move || unsafe {
            EVENTS.with(|slot| *slot.borrow_mut() = Some(tx));
            let mut k = SetWindowsHookExW(13, Some(keyboard), 0, 0);
            let mut m = SetWindowsHookExW(14, Some(mouse), 0, 0);
            if k == 0 || m == 0 {
                if k != 0 {
                    UnhookWindowsHookEx(k);
                }
                if m != 0 {
                    UnhookWindowsHookEx(m);
                }
                let _ = ready_rx_tx.send(Err("Cannot install global input hooks".into()));
                return;
            }
            // Like the source's listener recovery, periodically replace our
            // own hooks in case Windows removed one during system pressure.
            let timer = SetTimer(0, 0, 300_000, None);
            let _ = ready_rx_tx.send(Ok(GetCurrentThreadId()));
            let mut msg: Msg = std::mem::zeroed();
            while GetMessageW(&mut msg, 0, 0, 0) > 0 {
                if msg.message == 0x113 && msg.wparam == timer {
                    let next_k = SetWindowsHookExW(13, Some(keyboard), 0, 0);
                    let next_m = SetWindowsHookExW(14, Some(mouse), 0, 0);
                    if next_k != 0 {
                        UnhookWindowsHookEx(k);
                        k = next_k;
                    }
                    if next_m != 0 {
                        UnhookWindowsHookEx(m);
                        m = next_m;
                    }
                    send(vec!["hooks_reset".into()]);
                    continue;
                }
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
            UnhookWindowsHookEx(k);
            UnhookWindowsHookEx(m);
            if timer != 0 {
                KillTimer(0, timer);
            }
        });
        Ok(Self {
            thread_id: ready.recv().map_err(|e| e.to_string())??,
            join: Some(join),
        })
    }
}
impl Drop for Hooks {
    fn drop(&mut self) {
        unsafe {
            PostThreadMessageW(self.thread_id, 0x12, 0, 0);
        }
        if let Some(j) = self.join.take() {
            let _ = j.join();
        }
    }
}
pub fn dpi_aware() {
    unsafe {
        SetProcessDPIAware();
    }
}
pub fn documents() -> Option<std::path::PathBuf> {
    #[link(name = "shell32")]
    extern "system" {
        fn SHGetFolderPathW(
            hwnd: isize,
            folder: i32,
            token: isize,
            flags: u32,
            path: *mut u16,
        ) -> i32;
    }
    unsafe {
        let mut path = [0u16; 260];
        if SHGetFolderPathW(0, 5, 0, 0, path.as_mut_ptr()) == 0 {
            Some(
                String::from_utf16_lossy(&path[..path.iter().position(|v| *v == 0).unwrap_or(260)])
                    .into(),
            )
        } else {
            None
        }
    }
}
pub fn hwnd(child: isize) -> isize {
    unsafe {
        let p = GetParent(child);
        if p == 0 {
            child
        } else {
            p
        }
    }
}
pub fn style_overlay(child: isize, clickthrough: bool) {
    unsafe {
        let h = hwnd(child);
        let mut style = GetWindowLongPtrW(h, -20) | 0x80000 | 0x08000000 | 0x80;
        if clickthrough {
            style |= 0x20;
        } else {
            style &= !0x20;
        }
        SetWindowLongPtrW(h, -20, style);
        SetWindowPos(h, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10);
    }
}
pub fn topmost(child: isize) {
    unsafe {
        SetWindowPos(hwnd(child), -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10);
    }
}
pub fn bounds(child: isize) -> Rect {
    unsafe {
        let mut r = Rect::default();
        GetWindowRect(hwnd(child), &mut r);
        r
    }
}
pub fn position(child: isize, x: i32, y: i32) {
    unsafe {
        SetWindowPos(hwnd(child), -1, x, y, 0, 0, 0x1 | 0x10);
    }
}
unsafe extern "system" fn monitor(m: isize, _dc: isize, _r: *mut Rect, data: isize) -> i32 {
    let out = &mut *(data as *mut Vec<Rect>);
    let mut info = MonitorInfo {
        size: std::mem::size_of::<MonitorInfo>() as u32,
        monitor: Rect::default(),
        work: Rect::default(),
        flags: 0,
    };
    if GetMonitorInfoW(m, &mut info) != 0 {
        out.push(info.work);
    }
    1
}
pub fn monitors() -> Vec<Rect> {
    let mut list: Vec<Rect> = Vec::new();
    unsafe {
        EnumDisplayMonitors(
            0,
            std::ptr::null(),
            Some(monitor),
            &mut list as *mut _ as isize,
        );
    }
    list
}
pub fn clamp(x: i32, y: i32, w: i32, h: i32, areas: &[Rect]) -> (i32, i32) {
    areas
        .iter()
        .filter(|a| a.right > a.left && a.bottom > a.top)
        .map(|a| {
            (
                x.clamp(a.left, (a.right - w.max(1)).max(a.left)),
                y.clamp(a.top, (a.bottom - h.max(1)).max(a.top)),
            )
        })
        .min_by_key(|(cx, cy)| {
            let dx = *cx as i64 - x as i64;
            let dy = *cy as i64 - y as i64;
            dx * dx + dy * dy
        })
        .unwrap_or((x, y))
}
pub fn held(name: &str) -> bool {
    let vk = match name {
        "alt" => 0xa4,
        "right alt" => 0xa5,
        "ctrl" => 0x11,
        "shift" => 0xa0,
        "right shift" => 0xa1,
        _ => return false,
    };
    unsafe { GetAsyncKeyState(vk) < 0 }
}
pub fn idle() -> bool {
    unsafe {
        let mut i = LastInput {
            size: std::mem::size_of::<LastInput>() as u32,
            tick: 0,
        };
        GetLastInputInfo(&mut i) != 0 && GetTickCount().wrapping_sub(i.tick) > 3_600_000
    }
}
pub fn message(text: &str) {
    let t: Vec<u16> = text.encode_utf16().chain(Some(0)).collect();
    let title: Vec<u16> = "MusicApp Rust".encode_utf16().chain(Some(0)).collect();
    unsafe {
        MessageBoxW(0, t.as_ptr(), title.as_ptr(), 0x10);
    }
}
// Game detection never changes the priority or configuration of other applications.
pub fn start_game_monitor(
    tx: Sender<Vec<String>>,
    stop: Arc<AtomicBool>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        while !stop.load(Ordering::Relaxed) {
            let gaming = game_running();
            let _ = tx.send(vec!["game".into(), gaming.to_string()]);
            thread::park_timeout(Duration::from_secs(10));
        }
    })
}
pub fn game_running() -> bool {
    #[repr(C)]
    struct Process {
        size: u32,
        usage: u32,
        pid: u32,
        heap: usize,
        module: u32,
        threads: u32,
        parent: u32,
        priority: i32,
        flags: u32,
        exe: [u16; 260],
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> isize;
        fn Process32FirstW(h: isize, p: *mut Process) -> i32;
        fn Process32NextW(h: isize, p: *mut Process) -> i32;
        fn CloseHandle(h: isize) -> i32;
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> isize;
        fn QueryFullProcessImageNameW(
            process: isize,
            flags: u32,
            name: *mut u16,
            size: *mut u32,
        ) -> i32;
    }
    unsafe {
        let h = CreateToolhelp32Snapshot(2, 0);
        if h == -1 {
            return false;
        }
        let mut p: Process = std::mem::zeroed();
        p.size = std::mem::size_of::<Process>() as u32;
        let mut ok = Process32FirstW(h, &mut p);
        let mut result = false;
        while ok != 0 {
            let n = String::from_utf16_lossy(
                &p.exe[..p.exe.iter().position(|&c| c == 0).unwrap_or(260)],
            )
            .to_lowercase();
            let launcher = [
                "steam.exe",
                "steamwebhelper.exe",
                "epicgameslauncher.exe",
                "upc.exe",
                "ubisoftconnect.exe",
                "eadesktop.exe",
            ]
            .contains(&n.as_str());
            let mut game = n.starts_with("gta5") || n.starts_with("farcry") || n.starts_with("fc6");
            if !launcher && !game && p.pid != std::process::id() {
                let process = OpenProcess(0x1000, 0, p.pid);
                if process != 0 {
                    let mut path = [0u16; 1024];
                    let mut size = path.len() as u32;
                    if QueryFullProcessImageNameW(process, 0, path.as_mut_ptr(), &mut size) != 0 {
                        let path = String::from_utf16_lossy(&path[..size as usize])
                            .replace('\\', "/")
                            .to_lowercase();
                        game = [
                            "/steamapps/common/",
                            "/epic games/",
                            "/ubisoft game launcher/games/",
                            "/ea games/",
                            "/games/",
                        ]
                        .iter()
                        .any(|part| path.contains(part));
                    }
                    CloseHandle(process);
                }
            }
            if !launcher && game {
                result = true;
                break;
            }
            ok = Process32NextW(h, &mut p);
        }
        CloseHandle(h);
        result
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn clamps_negative_monitors() {
        let a = [Rect {
            left: -1920,
            top: 0,
            right: 0,
            bottom: 1080,
        }];
        assert_eq!(clamp(-50000, 50000, 400, 100, &a), (-1920, 980));
    }
    #[test]
    fn distinguishes_alt_sides() {
        assert_eq!(key_name(0xa4, 0, 0), "alt");
        assert_eq!(key_name(0xa5, 0, 0), "right alt");
    }
}
