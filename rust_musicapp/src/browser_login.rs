//! Chrome/Edge sign-in with an app-owned, remembered browser profile.
use serde_json::{json, Value};
use std::{
    fs,
    net::TcpListener,
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::{Duration, Instant},
};

pub fn protect(data: &[u8], decrypt: bool) -> Result<Vec<u8>, String> {
    #[repr(C)]
    struct Blob {
        size: u32,
        data: *mut u8,
    }
    #[link(name = "crypt32")]
    extern "system" {
        fn CryptProtectData(
            input: *const Blob,
            description: *const u16,
            entropy: *const Blob,
            reserved: isize,
            prompt: isize,
            flags: u32,
            output: *mut Blob,
        ) -> i32;
        fn CryptUnprotectData(
            input: *const Blob,
            description: *mut *mut u16,
            entropy: *const Blob,
            reserved: isize,
            prompt: isize,
            flags: u32,
            output: *mut Blob,
        ) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn LocalFree(value: isize) -> isize;
    }
    let input = Blob {
        size: data.len().try_into().map_err(|_| "Session too large")?,
        data: data.as_ptr() as *mut u8,
    };
    let mut output = Blob {
        size: 0,
        data: std::ptr::null_mut(),
    };
    unsafe {
        let ok = if decrypt {
            CryptUnprotectData(
                &input,
                std::ptr::null_mut(),
                std::ptr::null(),
                0,
                0,
                1,
                &mut output,
            )
        } else {
            CryptProtectData(
                &input,
                std::ptr::null(),
                std::ptr::null(),
                0,
                0,
                1,
                &mut output,
            )
        };
        if ok == 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
        let result = std::slice::from_raw_parts(output.data, output.size as usize).to_vec();
        LocalFree(output.data as isize);
        Ok(result)
    }
}
fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(Some(0)).collect()
}
fn registry(root: isize, key: &str, name: &str) -> Result<String, String> {
    #[link(name = "advapi32")]
    extern "system" {
        fn RegGetValueW(
            root: isize,
            key: *const u16,
            name: *const u16,
            flags: u32,
            kind: *mut u32,
            data: *mut std::ffi::c_void,
            size: *mut u32,
        ) -> i32;
    }
    let key = wide(key);
    let name = wide(name);
    let mut size = 0;
    unsafe {
        let code = RegGetValueW(
            root,
            key.as_ptr(),
            name.as_ptr(),
            6,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut size,
        );
        if code != 0 {
            return Err(format!("Cannot read the Windows default browser ({code})"));
        }
        let mut out = vec![0u16; size as usize / 2 + 1];
        let code = RegGetValueW(
            root,
            key.as_ptr(),
            name.as_ptr(),
            6,
            std::ptr::null_mut(),
            out.as_mut_ptr() as *mut _,
            &mut size,
        );
        if code != 0 {
            return Err("Cannot read the Windows default browser".into());
        }
        Ok(String::from_utf16_lossy(
            &out[..out.iter().position(|v| *v == 0).unwrap_or(out.len())],
        ))
    }
}
fn default_browser() -> Result<PathBuf, String> {
    let requested = std::env::var("MUSICAPP_BROWSER")
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !requested.is_empty() {
        return installed_browser(&requested).ok_or_else(|| {
            "MUSICAPP_BROWSER must name an installed Chrome or Edge browser".into()
        });
    }
    let id = registry(
        0x80000001u32 as i32 as isize,
        r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        "ProgId",
    )
    .unwrap_or_else(|_| "https".into());
    let command = registry(
        0x80000000u32 as i32 as isize,
        &format!(r"{id}\shell\open\command"),
        "",
    )
    .unwrap_or_default();
    let value = command.trim();
    let path = if let Some(rest) = value.strip_prefix('"') {
        rest.split('"').next().unwrap_or("").to_owned()
    } else {
        let lower = value.to_ascii_lowercase();
        lower
            .find(".exe")
            .map(|end| value[..end + 4].to_owned())
            .unwrap_or_default()
    };
    let path = PathBuf::from(path);
    let identity = (path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_lowercase()
        + " "
        + &id.to_lowercase())
        .to_lowercase();
    if path.is_absolute()
        && path.is_file()
        && (identity.contains("chrome") || identity.contains("msedge"))
    {
        return Ok(path);
    }
    installed_browser("edge")
        .or_else(|| installed_browser("chrome"))
        .ok_or_else(|| "Install Chrome or Microsoft Edge to sign in to YouTube".into())
}
fn installed_browser(name: &str) -> Option<PathBuf> {
    let relative = match name {
        "chrome" => "Google/Chrome/Application/chrome.exe",
        "edge" | "msedge" => "Microsoft/Edge/Application/msedge.exe",
        _ => return None,
    };
    ["LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"]
        .iter()
        .filter_map(std::env::var_os)
        .map(PathBuf::from)
        .map(|root| root.join(relative))
        .find(|path| path.is_file())
}
pub fn open_existing_profile_page(url: &str) -> Result<(), String> {
    let executable = default_browser()?;
    use std::os::windows::process::CommandExt;
    std::process::Command::new(executable)
        .args(["--new-tab", url])
        .creation_flags(0x08000000)
        .spawn()
        .map_err(|_| "Could not open Chrome or Edge".to_string())?;
    Ok(())
}
pub fn forget_owned_profile(data: &Path) -> Result<(), String> {
    use std::os::windows::fs::MetadataExt;
    let profile = data.join("browser-profile");
    let metadata = match profile.symlink_metadata() {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.to_string()),
    };
    if !metadata.is_dir() || metadata.file_attributes() & 0x400 != 0 {
        return Err("The app-owned browser profile is not a regular directory".into());
    }
    fs::remove_dir_all(profile).map_err(|_| {
        "Close the MusicApp sign-in window before forgetting its browser session".into()
    })
}

#[repr(C)]
struct Startup {
    cb: u32,
    reserved: *mut u16,
    desktop: *mut u16,
    title: *mut u16,
    x: u32,
    y: u32,
    xsize: u32,
    ysize: u32,
    xchars: u32,
    ychars: u32,
    fill: u32,
    flags: u32,
    show: u16,
    extra: u16,
    bytes: *mut u8,
    input: isize,
    output: isize,
    error: isize,
}
#[repr(C)]
struct ProcessInfo {
    process: isize,
    thread: isize,
    pid: u32,
    tid: u32,
}
#[repr(C)]
struct BasicLimits {
    process_time: i64,
    job_time: i64,
    flags: u32,
    min: usize,
    max: usize,
    active: u32,
    affinity: usize,
    priority: u32,
    scheduling: u32,
}
#[repr(C)]
struct IoCounters {
    values: [u64; 6],
}
#[repr(C)]
struct Limits {
    basic: BasicLimits,
    io: IoCounters,
    process_memory: usize,
    job_memory: usize,
    peak_process: usize,
    peak_job: usize,
}
#[link(name = "kernel32")]
extern "system" {
    fn CreateJobObjectW(attributes: isize, name: *const u16) -> isize;
    fn SetInformationJobObject(
        job: isize,
        class: u32,
        information: *const std::ffi::c_void,
        length: u32,
    ) -> i32;
    fn CreateProcessW(
        app: *const u16,
        command: *mut u16,
        process_attributes: isize,
        thread_attributes: isize,
        inherit: i32,
        flags: u32,
        environment: isize,
        directory: *const u16,
        startup: *const Startup,
        information: *mut ProcessInfo,
    ) -> i32;
    fn AssignProcessToJobObject(job: isize, process: isize) -> i32;
    fn ResumeThread(thread: isize) -> u32;
    fn TerminateProcess(process: isize, code: u32) -> i32;
    fn CloseHandle(handle: isize) -> i32;
    fn WaitForSingleObject(handle: isize, time: u32) -> u32;
}
struct OwnedBrowser {
    job: isize,
    process: isize,
}
impl OwnedBrowser {
    fn start(executable: &Path, profile: PathBuf, port: u16) -> Result<Self, String> {
        unsafe {
            let job = CreateJobObjectW(0, std::ptr::null());
            if job == 0 {
                return Err("Windows could not create a protected browser job".into());
            }
            let mut result = Self { job, process: 0 };
            let mut limits: Limits = std::mem::zeroed();
            limits.basic.flags = 0x2000;
            if SetInformationJobObject(
                job,
                9,
                &limits as *const _ as *const _,
                std::mem::size_of::<Limits>() as u32,
            ) == 0
            {
                return Err("Windows could not protect the sign-in browser".into());
            }
            let command = signin_command(executable, &profile, port);
            let app = wide(&executable.to_string_lossy());
            let mut command = wide(&command);
            let mut startup: Startup = std::mem::zeroed();
            startup.cb = std::mem::size_of::<Startup>() as u32;
            startup.flags = 0x1; // STARTF_USESHOWWINDOW
            startup.show = 3; // SW_SHOWMAXIMIZED; not F11/exclusive fullscreen
            let mut info: ProcessInfo = std::mem::zeroed();
            if CreateProcessW(
                app.as_ptr(),
                command.as_mut_ptr(),
                0,
                0,
                0,
                0x4 | 0x4000 | 0x08000000,
                0,
                std::ptr::null(),
                &startup,
                &mut info,
            ) == 0
            {
                return Err("Could not open the default browser".into());
            }
            result.process = info.process;
            if AssignProcessToJobObject(job, info.process) == 0 {
                TerminateProcess(info.process, 1);
                CloseHandle(info.thread);
                return Err("Windows could not contain the sign-in browser safely".into());
            }
            let resumed = ResumeThread(info.thread);
            CloseHandle(info.thread);
            if resumed == u32::MAX {
                return Err("Could not start the sign-in browser".into());
            }
            Ok(result)
        }
    }
}

fn signin_command(executable: &Path, profile: &Path, port: u16) -> String {
    format!("\"{}\" --user-data-dir=\"{}\" --remote-debugging-port={port} --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check --start-maximized --new-window \"https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2Fplaylist%3Flist%3DLL\"", executable.display(), profile.display())
}

fn maximize_owned_window(browser_endpoint: &str, page_endpoint: &str) -> Result<(), String> {
    let window = rpc(page_endpoint, "Browser.getWindowForTarget", json!({}))?;
    let id = window["windowId"]
        .as_i64()
        .ok_or("The browser did not identify its sign-in window")?;
    rpc(
        browser_endpoint,
        "Browser.setWindowBounds",
        json!({"windowId":id,"bounds":{"windowState":"maximized"}}),
    )?;
    Ok(())
}
impl Drop for OwnedBrowser {
    fn drop(&mut self) {
        unsafe {
            if self.job != 0 {
                CloseHandle(self.job);
            }
            if self.process != 0 {
                WaitForSingleObject(self.process, 1000);
                CloseHandle(self.process);
            }
        }
    }
}

fn rpc(endpoint: &str, method: &str, params: Value) -> Result<Value, String> {
    let url = url::Url::parse(endpoint).map_err(|e| e.to_string())?;
    if url.scheme() != "ws" || !matches!(url.host_str(), Some("127.0.0.1" | "localhost")) {
        return Err("Invalid local browser endpoint".into());
    }
    let (mut socket, _) = tungstenite::connect(endpoint).map_err(|e| e.to_string())?;
    if let tungstenite::stream::MaybeTlsStream::Plain(stream) = socket.get_mut() {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    }
    socket
        .send(tungstenite::Message::Text(
            json!({"id":1,"method":method,"params":params}).to_string(),
        ))
        .map_err(|e| e.to_string())?;
    loop {
        let message = socket.read().map_err(|e| e.to_string())?;
        if let tungstenite::Message::Text(text) = message {
            if text.len() > 2 * 1024 * 1024 {
                return Err("Browser response too large".into());
            }
            let value: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
            if value["id"] == 1 {
                if value.get("error").is_some() {
                    return Err("Browser command failed".into());
                }
                return Ok(value["result"].clone());
            }
        }
    }
}
pub fn login(data: &Path, cancel: &AtomicBool) -> Result<String, String> {
    let executable = default_browser()?;
    let browser_name = if executable.file_name().is_some_and(|name| {
        name.to_string_lossy()
            .to_ascii_lowercase()
            .contains("msedge")
    }) {
        "edge"
    } else {
        "chrome"
    };
    let profile = data.join("browser-profile").join(browser_name);
    fs::create_dir_all(&profile).map_err(|e| e.to_string())?;
    let reservation = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let port = reservation.local_addr().map_err(|e| e.to_string())?.port();
    drop(reservation);
    let _owned = OwnedBrowser::start(&executable, profile, port)?;
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| e.to_string())?;
    let deadline = Instant::now() + Duration::from_secs(300);
    let mut endpoint = None;
    let mut maximized = false;
    while Instant::now() < deadline {
        if cancel.load(Ordering::Relaxed) {
            return Err("Sign-in stopped. Music is still paused.".into());
        }
        if endpoint.is_none() {
            if let Ok(response) = client
                .get(format!("http://127.0.0.1:{port}/json/version"))
                .send()
            {
                if let Ok(info) = response.json::<Value>() {
                    if let Some(value) = info["webSocketDebuggerUrl"].as_str() {
                        if url::Url::parse(value).ok().is_some_and(|u| {
                            u.host_str() == Some("127.0.0.1")
                                && u.port() == Some(port)
                                && u.path().starts_with("/devtools/browser/")
                        }) {
                            endpoint = Some(value.to_string());
                        }
                    }
                }
            }
        }
        if let Some(endpoint) = &endpoint {
            if let Ok(targets) = client
                .get(format!("http://127.0.0.1:{port}/json/list"))
                .send()
                .and_then(|r| r.json::<Value>())
            {
                if let Some(targets) = targets.as_array() {
                    for target in targets {
                        if !maximized && target["type"].as_str() == Some("page") {
                            if let Some(page) = target["webSocketDebuggerUrl"].as_str() {
                                maximized = maximize_owned_window(endpoint, page).is_ok();
                            }
                        }
                        let address = target["url"].as_str().unwrap_or("");
                        if address.contains("accounts.google.com")
                            && (address.contains("deniedsigninrejected")
                                || address.contains("/signin/rejected"))
                        {
                            return Err(
                                "Google declined this browser sign-in. Close the window and retry."
                                    .into(),
                            );
                        }
                        let youtube = url::Url::parse(address)
                            .ok()
                            .and_then(|u| u.host_str().map(str::to_owned))
                            .is_some_and(|h| h == "youtube.com" || h.ends_with(".youtube.com"));
                        if !youtube {
                            continue;
                        }
                        if let Some(page) = target["webSocketDebuggerUrl"].as_str() {
                            if let Ok(value) = rpc(
                                page,
                                "Runtime.evaluate",
                                json!({"expression":"Boolean(window.ytcfg && window.ytcfg.get('LOGGED_IN') === true && document.querySelector('#avatar-btn'))","returnByValue":true}),
                            ) {
                                if value["result"]["value"] == true {
                                    let cookies = rpc(
                                        page,
                                        "Network.getCookies",
                                        json!({"urls":["https://www.youtube.com/","https://music.youtube.com/"]}),
                                    )?;
                                    let mut signed = false;
                                    let mut values = Vec::new();
                                    if let Some(items) = cookies["cookies"].as_array() {
                                        for cookie in items {
                                            let domain = cookie["domain"]
                                                .as_str()
                                                .unwrap_or("")
                                                .trim_start_matches('.');
                                            if domain != "youtube.com"
                                                && !domain.ends_with(".youtube.com")
                                            {
                                                continue;
                                            }
                                            let name = cookie["name"].as_str().unwrap_or("");
                                            let value = cookie["value"].as_str().unwrap_or("");
                                            if matches!(
                                                name,
                                                "SAPISID"
                                                    | "__Secure-3PAPISID"
                                                    | "__Secure-1PAPISID"
                                            ) && !value.is_empty()
                                            {
                                                signed = true;
                                            }
                                            values.push(format!("{name}={value}"));
                                        }
                                    }
                                    if signed {
                                        let _ = rpc(endpoint, "Browser.close", json!({}));
                                        return Ok(values.join("; "));
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        for _ in 0..15 {
            if cancel.load(Ordering::Relaxed) {
                return Err("Sign-in stopped".into());
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    Err("YouTube sign-in timed out. Finish signing in, then retry.".into())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn signin_uses_normal_maximized_owned_window() {
        let command = signin_command(
            Path::new("browser.exe"),
            Path::new("state/browser-profile/edge"),
            9222,
        );
        assert!(command.contains("--start-maximized"));
        assert!(command.contains("--new-window"));
        assert!(command.contains("--user-data-dir=\"state/browser-profile/edge\""));
        assert!(!command.contains("--start-fullscreen"));
        assert!(!command.contains("--kiosk"));
    }
    #[test]
    fn dpapi_never_stores_plaintext() {
        let plain = b"synthetic session fixture";
        let encrypted = protect(plain, false).unwrap();
        assert_ne!(plain.as_slice(), encrypted.as_slice());
        assert_eq!(protect(&encrypted, true).unwrap(), plain);
    }
}
