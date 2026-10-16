//! Native Innertube/account/download orchestration. No script interpreter or
//! downloader executable is invoked. FFmpeg is used only as a native encoder.
use super::{
    audio_file_exists, budget_for, eligible_tracks, song_keys, DownloadBudget, Library,
    ServiceEvent, Track, DEFAULT_SOURCE, DOWNLOAD_COOLDOWN,
};
#[path = "browser_login.rs"]
mod browser_login;
pub fn open_browser_profile_page(url: &str) -> Result<(), String> {
    browser_login::open_existing_profile_page(url)
}
use rustypipe::{
    client::RustyPipe,
    model::{paginator::Paginator, traits::FromYtItem, TrackItem, VideoItem},
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, VecDeque},
    fs::{self, File},
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn emit(tx: &SyncSender<ServiceEvent>, name: &str, data: Value) {
    let event = ServiceEvent {
        event: name.into(),
        data: data.as_object().cloned().unwrap_or_default(),
    };
    if name == "status" {
        let _ = tx.try_send(event);
    } else {
        let _ = tx.send(event);
    }
}

/// Browser ownership begins only after the presentation thread has actually
/// withdrawn the app. The lease restores it on success, cancellation, or error.
fn with_browser_ui<T>(
    events: &SyncSender<ServiceEvent>,
    ready: &AtomicBool,
    cancel: &AtomicBool,
    closed: &AtomicBool,
    timeout: Duration,
    login: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    struct Lease<'a>(&'a SyncSender<ServiceEvent>);
    impl Drop for Lease<'_> {
        fn drop(&mut self) {
            emit(self.0, "browser_auth", json!({"active":false}));
        }
    }
    ready.store(false, Ordering::Release);
    emit(events, "browser_auth", json!({"active":true}));
    let _lease = Lease(events);
    let deadline = Instant::now() + timeout;
    loop {
        if cancel.load(Ordering::Relaxed) || closed.load(Ordering::Relaxed) {
            return Err("Sign-in stopped. Music is still paused.".into());
        }
        if ready.load(Ordering::Acquire) {
            break;
        }
        if Instant::now() >= deadline {
            return Err("MusicApp couldn't prepare the sign-in window. Retry setup.".into());
        }
        thread::sleep(Duration::from_millis(20));
    }
    login()
}
fn read_json<T: serde::de::DeserializeOwned + Default>(path: &Path) -> T {
    fs::read(path)
        .ok()
        .and_then(|v| serde_json::from_slice(&v).ok())
        .unwrap_or_default()
}
fn save_json(path: &Path, value: &impl Serialize) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let temporary = path.with_extension("json.tmp");
    fs::write(
        &temporary,
        serde_json::to_vec(value).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    fs::rename(temporary, path).map_err(|e| e.to_string())
}

struct ProtectedCache {
    path: PathBuf,
    memory: Mutex<Option<String>>,
    remembered: AtomicBool,
}
struct CacheBackend(Arc<ProtectedCache>);
impl rustypipe::cache::CacheStorage for CacheBackend {
    fn read(&self) -> Option<String> {
        if let Some(value) = self
            .0
            .memory
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
        {
            return Some(value);
        }
        String::from_utf8(browser_login::protect(&fs::read(&self.0.path).ok()?, true).ok()?).ok()
    }
    fn write(&self, data: &str) {
        *self.0.memory.lock().unwrap_or_else(|e| e.into_inner()) = Some(data.into());
        let result = browser_login::protect(data.as_bytes(), false).and_then(|bytes| {
            let temp = self.0.path.with_extension("bin.tmp");
            fs::write(&temp, bytes)
                .and_then(|_| fs::rename(temp, &self.0.path))
                .map_err(|e| e.to_string())
        });
        self.0.remembered.store(result.is_ok(), Ordering::Relaxed);
    }
}
#[derive(Default)]
struct Status {
    message: String,
    phase: String,
    busy: bool,
    connected: bool,
    ready: bool,
    total: Option<usize>,
    saved: usize,
    skipped: usize,
    deferred: usize,
    downloads: HashMap<String, Value>,
}
struct ResourceMonitor {
    checked: Instant,
    cpu: Option<(u64, u64)>,
    game_checked: Instant,
    gaming: bool,
    current: DownloadBudget,
    recovering: Option<(u64, Instant)>,
}
impl ResourceMonitor {
    fn new() -> Self {
        Self {
            checked: Instant::now() - Duration::from_secs(3),
            cpu: None,
            game_checked: Instant::now() - Duration::from_secs(11),
            gaming: false,
            current: budget_for(0, 100., 100., false),
            recovering: None,
        }
    }
    fn get(&mut self) -> DownloadBudget {
        if self.checked.elapsed() < Duration::from_secs(2) {
            return self.current.clone();
        }
        self.checked = Instant::now();
        #[repr(C)]
        struct Memory {
            size: u32,
            load: u32,
            total: u64,
            available: u64,
            page_total: u64,
            page_available: u64,
            virtual_total: u64,
            virtual_available: u64,
            extended: u64,
        }
        #[repr(C)]
        struct FileTime {
            low: u32,
            high: u32,
        }
        #[link(name = "kernel32")]
        extern "system" {
            fn GlobalMemoryStatusEx(value: *mut Memory) -> i32;
            fn GetSystemTimes(
                idle: *mut FileTime,
                kernel: *mut FileTime,
                user: *mut FileTime,
            ) -> i32;
        }
        let mut memory: Memory = unsafe { std::mem::zeroed() };
        memory.size = std::mem::size_of::<Memory>() as u32;
        let mut idle = FileTime { low: 0, high: 0 };
        let mut kernel = FileTime { low: 0, high: 0 };
        let mut user = FileTime { low: 0, high: 0 };
        let mut cpu = 0.;
        let convert = |t: FileTime| (t.high as u64) << 32 | t.low as u64;
        let ok = unsafe { GlobalMemoryStatusEx(&mut memory) != 0 };
        if unsafe { GetSystemTimes(&mut idle, &mut kernel, &mut user) } != 0 {
            let total = convert(kernel) + convert(user);
            let busy = total.saturating_sub(convert(idle));
            if let Some((old_total, old_busy)) = self.cpu {
                if total > old_total {
                    cpu = 100. * busy.saturating_sub(old_busy) as f64 / (total - old_total) as f64;
                }
            }
            self.cpu = Some((total, busy));
        }
        if self.game_checked.elapsed() >= Duration::from_secs(10) {
            self.gaming = crate::platform::game_running();
            self.game_checked = Instant::now();
        }
        let candidate = if ok {
            budget_for(memory.available, memory.load as f64, cpu, self.gaming)
        } else {
            budget_for(0, 100., 100., true)
        };
        if candidate.bytes_per_second <= self.current.bytes_per_second {
            self.current = candidate;
            self.recovering = None;
        } else if self.recovering.is_some_and(|(rate, at)| {
            rate == candidate.bytes_per_second && at.elapsed() >= Duration::from_secs(10)
        }) {
            self.current = candidate;
            self.recovering = None;
        } else if !self
            .recovering
            .is_some_and(|(rate, _)| rate == candidate.bytes_per_second)
        {
            self.recovering = Some((candidate.bytes_per_second, Instant::now()));
        }
        self.current.clone()
    }
}
struct Slots {
    active: usize,
    next: Instant,
}
struct Backend {
    root: PathBuf,
    data: PathBuf,
    pipe: RustyPipe,
    runtime: tokio::runtime::Runtime,
    http: reqwest::blocking::Client,
    stream_http: reqwest::Client,
    cancel: AtomicBool,
    closed: AtomicBool,
    authentication_ui_ready: AtomicBool,
    status: Mutex<Status>,
    cache: Arc<ProtectedCache>,
    events: SyncSender<ServiceEvent>,
    active_path: Mutex<Option<String>>,
    directories: Mutex<Vec<String>>,
    resources: Mutex<ResourceMonitor>,
    slots: Mutex<Slots>,
    metadata: Mutex<()>,
    sync_owner: Mutex<()>,
    video_locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
}
impl Backend {
    fn stopped(&self) -> bool {
        self.cancel.load(Ordering::Relaxed) || self.closed.load(Ordering::Relaxed)
    }
    fn check(&self) -> Result<(), String> {
        if self.stopped() {
            Err("Cancelled".into())
        } else {
            Ok(())
        }
    }
    fn wait(&self, duration: Duration) -> Result<(), String> {
        let end = Instant::now() + duration;
        while Instant::now() < end {
            self.check()?;
            thread::sleep(
                Duration::from_millis(50).min(end.saturating_duration_since(Instant::now())),
            );
        }
        self.check()
    }
    fn call<F, T, E>(&self, future: F) -> Result<T, String>
    where
        F: std::future::Future<Output = Result<T, E>>,
        E: std::fmt::Display,
    {
        self.runtime.block_on(async{tokio::pin!(future);loop{tokio::select!{result=&mut future=>return result.map_err(|e|e.to_string()),_=tokio::time::sleep(Duration::from_millis(100))=>{self.check()?;}}}})
    }
    fn status(&self, message: Option<&str>, phase: Option<&str>) {
        let mut s = self.status.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(message) = message {
            s.message = message.into();
        }
        if let Some(phase) = phase {
            s.phase = phase.into();
        }
        emit(
            &self.events,
            "status",
            json!({"message":s.message,"phase":s.phase,"busy":s.busy,"connected":s.connected,"ready":s.ready,"total":s.total,"saved":s.saved,"skipped":s.skipped,"deferred":s.deferred,"remembered":self.cache.remembered.load(Ordering::Relaxed),"downloads":s.downloads.values().collect::<Vec<_>>() }),
        );
    }
    fn progress(
        &self,
        id: &str,
        title: &str,
        phase: &str,
        processor: &str,
        downloaded: u64,
        total: u64,
    ) {
        let fraction = if matches!(phase, "processing" | "finished") {
            Some(1.)
        } else if total > 0 {
            Some((downloaded as f64 / total as f64).min(1.))
        } else {
            None
        };
        self.status.lock().unwrap_or_else(|e|e.into_inner()).downloads.insert(id.into(),json!({"id":id,"title":title,"phase":phase,"processor":processor,"progress":fraction,"downloaded_bytes":downloaded,"total_bytes":total}));
        self.status(None, None);
    }
    fn budget(&self) -> DownloadBudget {
        self.resources
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get()
    }
    fn acquire_slot(&self) -> Result<Slot<'_>, String> {
        loop {
            self.check()?;
            let budget = self.budget();
            {
                let mut slots = self.slots.lock().unwrap_or_else(|e| e.into_inner());
                if slots.active < budget.workers && Instant::now() >= slots.next {
                    slots.active += 1;
                    slots.next = Instant::now() + Duration::from_secs_f64(budget.gap);
                    return Ok(Slot(self));
                }
            }
            self.wait(Duration::from_millis(100))?;
        }
    }
    fn authenticated(&self, force: bool) -> Result<(), String> {
        self.check()?;
        if !force && self.call(self.pipe.user_auth_check_cookie()).is_ok() {
            self.status
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .connected = true;
            return Ok(());
        }
        self.status(
            Some("Sign in to YouTube in your default browser. Music will wait."),
            Some("browser"),
        );
        self.check()?;
        let cookies = with_browser_ui(
            &self.events,
            &self.authentication_ui_ready,
            &self.cancel,
            &self.closed,
            Duration::from_secs(5),
            || browser_login::login(&self.data, &self.cancel),
        )?;
        self.call(self.pipe.user_auth_set_cookie(cookies))?;
        self.call(self.pipe.user_auth_check_cookie())?;
        self.status
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .connected = true;
        self.status(
            Some("YouTube connected. Preparing your music…"),
            Some("connecting"),
        );
        Ok(())
    }
    fn pages<T: FromYtItem>(&self, mut page: Paginator<T>) -> Result<Vec<T>, String> {
        let mut result = Vec::new();
        let mut tokens = std::collections::HashSet::new();
        loop {
            self.check()?;
            result.append(&mut page.items);
            let Some(token) = page.ctoken.take() else {
                return Ok(result);
            };
            if !tokens.insert(token.clone()) {
                return Err(
                    "YouTube repeated a continuation token; source sync was stopped safely".into(),
                );
            }
            let mut query = self.pipe.query();
            if page.authenticated {
                query = query.authenticated();
            }
            page = self.call(query.continuation::<T, _>(
                token,
                page.endpoint,
                page.visitor_data.as_deref(),
            ))?;
        }
    }
    fn music_track(track: TrackItem) -> Track {
        Track {
            id: track.id.clone(),
            title: track.name,
            artist: track
                .artists
                .iter()
                .map(|a| a.name.as_str())
                .collect::<Vec<_>>()
                .join(", "),
            duration: track.duration.map(|v| v as f64),
            url: Some(format!("https://www.youtube.com/watch?v={}", track.id)),
            ..Default::default()
        }
    }
    fn video_track(track: VideoItem) -> Track {
        Track {
            id: track.id.clone(),
            title: track.name,
            artist: track
                .channel
                .map(|v| v.name)
                .unwrap_or_else(|| "YouTube".into()),
            duration: track.duration.map(|v| v as f64),
            url: Some(format!("https://www.youtube.com/watch?v={}", track.id)),
            ..Default::default()
        }
    }
    fn fetch_source(&self, source: &str) -> Result<Vec<Track>, String> {
        let _lock = self.metadata.lock().unwrap_or_else(|e| e.into_inner());
        if source == DEFAULT_SOURCE {
            let playlist = self.call(self.pipe.query().music_liked_tracks())?;
            Ok(self
                .pages(playlist.tracks)?
                .into_iter()
                .map(Self::music_track)
                .collect())
        } else {
            let playlist = self.call(self.pipe.query().liked_videos())?;
            Ok(self
                .pages(playlist.videos)?
                .into_iter()
                .map(Self::video_track)
                .collect())
        }
    }
    fn fetch_playlist(&self, url: &str) -> Result<Vec<Track>, String> {
        if !super::valid_playlist_url(url) {
            return Err("Invalid YouTube playlist URL".into());
        }
        let id = url::Url::parse(url)
            .map_err(|e| e.to_string())?
            .query_pairs()
            .find(|(key, _)| key == "list")
            .map(|(_, v)| v.into_owned())
            .ok_or("Missing playlist ID")?;
        let _lock = self.metadata.lock().unwrap_or_else(|e| e.into_inner());
        let playlist = self.call(self.pipe.query().authenticated().playlist(id))?;
        Ok(self
            .pages(playlist.videos)?
            .into_iter()
            .map(Self::video_track)
            .collect())
    }
    fn search(&self, query: &str, limit: usize) -> Result<Vec<Track>, String> {
        let _lock = self.metadata.lock().unwrap_or_else(|e| e.into_inner());
        let found = self.call(self.pipe.query().search::<VideoItem, _>(query))?;
        Ok(found
            .items
            .items
            .into_iter()
            .take(limit.min(20))
            .map(Self::video_track)
            .collect())
    }
    fn target_directory(&self, directories: &[String]) -> PathBuf {
        for game in ["gtav enhanced", "gta v"] {
            for name in directories {
                let path = Path::new(name);
                if path
                    .file_name()
                    .is_some_and(|v| v.to_string_lossy().eq_ignore_ascii_case("user music"))
                    && path
                        .parent()
                        .and_then(Path::file_name)
                        .is_some_and(|v| v.to_string_lossy().eq_ignore_ascii_case(game))
                {
                    return path.into();
                }
            }
        }
        directories
            .iter()
            .find(|v| !v.contains("://") && !v.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(std::env::var_os("USERPROFILE").unwrap_or_default())
                    .join("MusicPlayerYoutubeDownloads")
            })
    }
    fn download(
        &self,
        track: &Track,
        permanent: bool,
        directory: Option<&Path>,
    ) -> Result<Track, String> {
        self.check()?;
        let id = if super::valid_id(&track.id) {
            track.id.clone()
        } else {
            super::youtube_video_id(track.url.as_deref().unwrap_or(""))
                .ok_or("Invalid YouTube video URL")?
        };
        let target = if permanent {
            directory.map(Path::to_path_buf).unwrap_or_else(|| {
                self.target_directory(&self.directories.lock().unwrap_or_else(|e| e.into_inner()))
            })
        } else {
            self.data.clone()
        };
        fs::create_dir_all(&target).map_err(|e| e.to_string())?;
        let key = format!("{}|{}|{}", id, target.display(), permanent);
        let lock = self
            .video_locks
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .entry(key)
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone();
        let _lock = loop {
            if let Ok(lock) = lock.try_lock() {
                break lock;
            }
            self.wait(Duration::from_millis(100))?;
        };
        if !permanent {
            if let Some(path) = cache_find(&self.data, &id) {
                return super::track_from_path(path)
                    .ok_or("Cached audio metadata could not be read".into());
            }
        }
        if permanent {
            if let Ok(entries) = fs::read_dir(&target) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    if path
                        .extension()
                        .is_some_and(|v| v.eq_ignore_ascii_case("mp3"))
                        && path
                            .file_stem()
                            .is_some_and(|v| v.to_string_lossy().ends_with(&format!(" [{id}]")))
                        && audio_file_exists(&path)
                    {
                        return super::track_from_path(path)
                            .ok_or("Saved audio metadata could not be read".into());
                    }
                }
            }
        }
        let _slot = self.acquire_slot()?;
        self.progress(&id, &track.title, "preparing", "", 0, 0);
        let player=self.call(self.pipe.query().player(&id)).map_err(|e|format!("Native YouTube stream extraction failed: {e}. Some videos may require a PO token; this build does not fall back to a script downloader."))?;
        let stream = player
            .audio_streams
            .iter()
            .filter(|s| s.drm_track_type.is_none())
            .max_by_key(|s| s.bitrate)
            .ok_or("YouTube returned no usable native audio stream")?;
        let title = if !track.title.is_empty() {
            track.title.clone()
        } else {
            player.details.name.clone().unwrap_or_else(|| id.clone())
        };
        let artist = if !track.artist.is_empty() {
            track.artist.clone()
        } else {
            serde_json::to_value(&player.details)
                .ok()
                .and_then(|v| v["channel"]["name"].as_str().map(str::to_string))
                .unwrap_or_else(|| "YouTube".into())
        };
        let extension = if stream.mime.contains("mp4") {
            "m4a"
        } else if stream.mime.contains("webm") {
            "webm"
        } else {
            "opus"
        };
        let stem = if permanent {
            format!("{} [{id}]", safe_filename(&title))
        } else {
            format!(".youtubeCached [{id}]")
        };
        let final_path = target.join(format!(
            "{stem}.{}",
            if permanent { "mp3" } else { extension }
        ));
        let input = target.join(format!(".{id}.native-download.{extension}"));
        let result = (|| {
            let mut response = self
                .call(self.stream_http.get(&stream.url).send())?
                .error_for_status()
                .map_err(|e| e.to_string())?;
            let total = response.content_length().unwrap_or(stream.size);
            let mut output = File::create(&input).map_err(|e| e.to_string())?;
            let mut count = 0u64;
            let mut report = Instant::now() - Duration::from_secs(1);
            loop {
                self.check()?;
                let at = Instant::now();
                let Some(bytes) = self.call(response.chunk())? else {
                    break;
                };
                let amount = bytes.len();
                output.write_all(&bytes).map_err(|e| e.to_string())?;
                count += amount as u64;
                if report.elapsed() >= Duration::from_millis(250) {
                    self.progress(&id, &title, "downloading", "", count, total);
                    report = Instant::now();
                }
                let active = self
                    .slots
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .active
                    .max(1) as u64;
                let rate = (self.budget().bytes_per_second / active).max(16 * 1024);
                self.wait(
                    Duration::from_secs_f64(amount as f64 / rate as f64)
                        .saturating_sub(at.elapsed()),
                )?;
            }
            output.flush().map_err(|e| e.to_string())?;
            drop(output);
            self.check()?;
            if permanent {
                self.progress(&id, &title, "processing", "SponsorBlock", count, total);
                let cuts = self.sponsor_cuts(&id);
                self.progress(
                    &id,
                    &title,
                    "processing",
                    if cuts.is_empty() {
                        "FFmpegExtractAudio"
                    } else {
                        "ModifyChapters"
                    },
                    count,
                    total,
                );
                let temporary = target.join(format!(".{id}.native-output.mp3"));
                let mut command = Command::new(crate::audio::find_tool(&self.root, "ffmpeg"));
                command
                    .args([
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-threads",
                        "1",
                        "-filter_threads",
                        "1",
                        "-i",
                    ])
                    .arg(&input)
                    .args([
                        "-vn",
                        "-acodec",
                        "libmp3lame",
                        "-b:a",
                        "192k",
                        "-id3v2_version",
                        "3",
                        "-metadata",
                    ])
                    .arg(format!("title={title}"))
                    .arg("-metadata")
                    .arg(format!("artist={artist}"));
                if !cuts.is_empty() {
                    let expression = cuts
                        .iter()
                        .map(|(a, b)| format!("between(t,{a:.6},{b:.6})"))
                        .collect::<Vec<_>>()
                        .join("+");
                    command
                        .arg("-af")
                        .arg(format!("aselect='not({expression})',asetpts=N/SR/TB"));
                }
                command
                    .arg(&temporary)
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null());
                use std::os::windows::process::CommandExt;
                command.creation_flags(0x08000000 | 0x4000);
                let mut child = command.spawn().map_err(|e| e.to_string())?;
                let success = loop {
                    // Change only our encoder's priority as system pressure changes.
                    use std::os::windows::io::AsRawHandle;
                    #[link(name = "kernel32")]
                    extern "system" {
                        fn SetPriorityClass(process: isize, class: u32) -> i32;
                    }
                    unsafe {
                        SetPriorityClass(
                            child.as_raw_handle() as isize,
                            if self.budget().limited { 0x40 } else { 0x4000 },
                        );
                    }
                    if self.stopped() {
                        let _ = child.kill();
                        let _ = child.wait();
                        let _ = fs::remove_file(&temporary);
                        return Err("Cancelled".into());
                    }
                    if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                        break status.success();
                    }
                    thread::sleep(Duration::from_millis(100));
                };
                if !success {
                    let _ = fs::remove_file(temporary);
                    return Err("FFmpeg could not convert the downloaded song".into());
                }
                fs::rename(temporary, &final_path).map_err(|e| e.to_string())?;
            } else {
                fs::rename(&input, &final_path).map_err(|e| e.to_string())?;
            }
            self.check()?;
            let mut saved = super::track_from_path(&final_path)
                .ok_or("Downloaded audio metadata could not be read")?;
            saved.id = id.clone();
            if saved.artist == "Unknown Artist" {
                saved.artist = artist;
            }
            saved.title = title.clone();
            self.progress(&id, &title, "finished", "", count, total);
            if permanent {
                emit(&self.events, "track", json!({"track":saved}));
            } else {
                cache_prune(
                    &self.data,
                    self.active_path
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_deref(),
                    Some(&final_path),
                );
            }
            Ok(saved)
        })();
        let _ = fs::remove_file(input);
        self.status
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .downloads
            .remove(&id);
        self.status(None, None);
        result
    }
    fn sponsor_cuts(&self, id: &str) -> Vec<(f64, f64)> {
        let categories = json!([
            "sponsor",
            "intro",
            "outro",
            "selfpromo",
            "preview",
            "filler",
            "music_offtopic"
        ])
        .to_string();
        let response = self
            .http
            .get("https://sponsor.ajay.app/api/skipSegments")
            .query(&[("videoID", id), ("categories", categories.as_str())])
            .timeout(Duration::from_secs(8))
            .send();
        if let Ok(response) = response {
            if let Ok(values) = response.json::<Value>() {
                let cuts = values
                    .as_array()
                    .map(|v| {
                        v.iter()
                            .filter_map(|v| {
                                Some((v["segment"][0].as_f64()?, v["segment"][1].as_f64()?))
                            })
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default();
                return merge_cuts(cuts);
            }
        }
        Vec::new()
    }
}
struct Slot<'a>(&'a Backend);
impl Drop for Slot<'_> {
    fn drop(&mut self) {
        self.0
            .slots
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .active -= 1;
    }
}
fn safe_filename(value: &str) -> String {
    let mut s = value
        .chars()
        .take(100)
        .map(|c| {
            if c.is_control() || "<>:\"/\\|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .collect::<String>()
        .trim_end_matches([' ', '.'])
        .to_string();
    if s.is_empty() {
        s = "YouTube".into();
    }
    s
}
fn merge_cuts(mut cuts: Vec<(f64, f64)>) -> Vec<(f64, f64)> {
    cuts.retain(|(a, b)| a.is_finite() && b.is_finite() && *a >= 0. && b > a);
    cuts.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut result: Vec<(f64, f64)> = Vec::new();
    for (a, b) in cuts {
        if let Some(last) = result.last_mut() {
            if a <= last.1 {
                last.1 = last.1.max(b);
                continue;
            }
        }
        result.push((a, b));
    }
    result
}
fn cache_entries(directory: &Path) -> Vec<(PathBuf, fs::Metadata)> {
    let Ok(entries) = fs::read_dir(directory) else {
        return vec![];
    };
    static NAME: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(
            r"(?i)^\.youtubeCached \[[A-Za-z0-9_-]{11}\]\.(mp3|m4a|webm|opus|ogg|wav|flac)$",
        )
        .unwrap()
    });
    entries
        .flatten()
        .filter_map(|e| {
            if !NAME.is_match(&e.file_name().to_string_lossy()) || !e.file_type().ok()?.is_file() {
                return None;
            }
            let metadata = e.metadata().ok()?;
            use std::os::windows::fs::MetadataExt;
            if metadata.len() == 0 || metadata.file_attributes() & 0x400 != 0 {
                return None;
            }
            Some((e.path(), metadata))
        })
        .collect()
}
fn cache_find(directory: &Path, id: &str) -> Option<PathBuf> {
    let mut entries = cache_entries(directory);
    entries.retain(|(p, _)| {
        p.file_stem()
            .is_some_and(|v| v == format!(".youtubeCached [{id}]").as_str())
    });
    entries.sort_by_key(|(_, m)| {
        std::cmp::Reverse(
            m.accessed()
                .unwrap_or(UNIX_EPOCH)
                .max(m.modified().unwrap_or(UNIX_EPOCH)),
        )
    });
    let path = entries.first()?.0.clone();
    if let Ok(file) = File::open(&path) {
        let _ = file.set_times(fs::FileTimes::new().set_accessed(SystemTime::now()));
    }
    Some(path)
}
fn cache_prune(directory: &Path, active: Option<&str>, incoming: Option<&Path>) {
    let mut entries = cache_entries(directory);
    let mut bytes = entries.iter().map(|(_, m)| m.len()).sum::<u64>();
    let mut count = entries.len();
    entries.sort_by_key(|(_, m)| {
        m.accessed()
            .unwrap_or(UNIX_EPOCH)
            .max(m.modified().unwrap_or(UNIX_EPOCH))
    });
    for (path, meta) in entries {
        if count <= 8 && bytes <= 64 * 1024 * 1024 {
            break;
        }
        if incoming.is_some_and(|p| p == path) || active.is_some_and(|p| Path::new(p) == path) {
            continue;
        }
        if fs::remove_file(path).is_ok() {
            count -= 1;
            bytes = bytes.saturating_sub(meta.len());
        }
    }
}

#[derive(Serialize, Deserialize)]
struct Deferred {
    title: String,
    retry_after: f64,
}
fn groups(tracks: Vec<Track>) -> Vec<Vec<Track>> {
    let mut groups: Vec<Vec<Track>> = Vec::new();
    let mut identities = HashMap::new();
    for track in tracks {
        let keys = song_keys(&track);
        let group = keys
            .iter()
            .find_map(|k| identities.get(k).copied())
            .unwrap_or_else(|| {
                groups.push(Vec::new());
                groups.len() - 1
            });
        groups[group].push(track);
        for key in keys {
            identities.insert(key, group);
        }
    }
    groups
}
impl Backend {
    fn sync(self: &Arc<Self>, command: &Value) -> Result<(), String> {
        let _owner = self
            .sync_owner
            .try_lock()
            .map_err(|_| "YouTube is already busy")?;
        self.cancel.store(false, Ordering::Relaxed);
        {
            let mut s = self.status.lock().unwrap_or_else(|e| e.into_inner());
            s.busy = true;
            s.ready = false;
            s.total = None;
            s.saved = 0;
            s.skipped = 0;
            s.deferred = 0;
        }
        let result: Result<(), String> = (|| {
            self.status(
                Some("Preparing native YouTube support…"),
                Some("connecting"),
            );
            self.authenticated(command["login"].as_bool().unwrap_or(false))?;
            let source = command["source"]
                .as_str()
                .filter(|s| super::SOURCES.contains(s))
                .unwrap_or(DEFAULT_SOURCE);
            self.status(Some("Checking your YouTube likes…"), Some("connecting"));
            let mut tracks = self.fetch_source(source)?;
            let mut likes: Value = read_json(&self.data.join(".youtube_likes.json"));
            if !likes.is_object() {
                likes = json!({});
            }
            if !likes["sources"].is_object() {
                likes["sources"] = json!({});
            }
            likes["sources"][source] = json!({"fetched_at":now(),"tracks":tracks});
            let directories: Vec<String> = command["directories"]
                .as_array()
                .map(|v| {
                    v.iter()
                        .filter_map(|s| s.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default();
            *self.directories.lock().unwrap_or_else(|e| e.into_inner()) = directories.clone();
            save_json(&self.data.join(".native_directories.json"), &directories)?;
            let lists: Vec<String> = command["playlists"]
                .as_array()
                .map(|v| {
                    v.iter()
                        .filter_map(|s| s.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default();
            let mut seen = std::collections::HashSet::new();
            for (index, url) in lists
                .iter()
                .filter(|url| seen.insert((*url).clone()))
                .enumerate()
            {
                self.check()?;
                if !super::valid_playlist_url(url) {
                    self.status
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .skipped += 1;
                    continue;
                }
                self.status(
                    Some(&format!("Checking playlist {}…", index + 1)),
                    Some("connecting"),
                );
                let entries = self.fetch_playlist(url)?;
                likes["sources"][url] = json!({"fetched_at":now(),"tracks":entries});
                tracks.extend(entries);
            }
            self.check()?;
            save_json(&self.data.join(".youtube_likes.json"), &likes)?;
            let directory = self.target_directory(&directories);
            fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
            self.status(
                Some("Checking songs already in your library…"),
                Some("syncing"),
            );
            let mut library = Library::default();
            for track in
                super::scan_library_cached(&directories, &self.data.join(".sync_metadata.json"))
            {
                self.check()?;
                library.add(&track);
            }
            let mut downloaded: HashMap<String, String> =
                read_json(&self.data.join(".youtube_downloads.json"));
            let mut deferred: HashMap<String, Deferred> =
                read_json(&self.data.join(".youtube_deferred_downloads.json"));
            deferred.retain(|_, v| v.retry_after.is_finite() && v.retry_after > now());
            let (eligible, skipped) = eligible_tracks(tracks);
            let groups = groups(eligible);
            {
                let mut s = self.status.lock().unwrap_or_else(|e| e.into_inner());
                s.total = Some(groups.len());
                s.skipped += skipped;
            }
            let mut queue: VecDeque<_> = groups.into_iter().map(|g| (g, 1usize)).collect();
            let (done_tx, done_rx) = mpsc::channel();
            let mut active = 0usize;
            let mut completed = 0;
            let mut checkpoint = Instant::now();
            while !queue.is_empty() || active > 0 {
                if self.stopped() {
                    queue.clear();
                }
                let budget = self.budget();
                while !queue.is_empty() && active < budget.workers && !self.stopped() {
                    let (group, attempt) = queue.pop_front().unwrap();
                    if let Some(path) = group
                        .iter()
                        .find_map(|entry| library.find(entry, &downloaded, Some(&directory)))
                    {
                        for entry in &group {
                            downloaded.insert(entry.id.clone(), path.clone());
                            deferred.remove(&entry.id);
                            let mut t = entry.clone();
                            t.path = path.clone();
                            library.add(&t);
                        }
                        emit(
                            &self.events,
                            "track",
                            json!({"track":super::track_from_path(&path)}),
                        );
                        self.status.lock().unwrap_or_else(|e| e.into_inner()).saved += 1;
                        save_json(&self.data.join(".youtube_downloads.json"), &downloaded)?;
                        continue;
                    }
                    let available: Vec<_> = group
                        .iter()
                        .filter(|t| !deferred.contains_key(&t.id))
                        .cloned()
                        .collect();
                    if available.is_empty() {
                        let mut s = self.status.lock().unwrap_or_else(|e| e.into_inner());
                        s.deferred += 1;
                        s.total = s.total.map(|n| n.saturating_sub(1));
                        continue;
                    }
                    let entry = available[(attempt - 1) % available.len()].clone();
                    let tx = done_tx.clone();
                    let backend = self.clone();
                    let directory = directory.clone();
                    active += 1;
                    thread::spawn(move || {
                        let result = backend.download(&entry, true, Some(&directory));
                        let _ = tx.send((group, attempt, result));
                    });
                }
                self.status(
                    Some(&{
                        let s = self.status.lock().unwrap_or_else(|e| e.into_inner());
                        format!(
                            "{} of {} songs ready · likes and playlists\n{} · up to {} at a time",
                            s.saved,
                            s.total.unwrap_or(0),
                            budget.label,
                            budget.workers
                        )
                    }),
                    Some("syncing"),
                );
                if active == 0 {
                    continue;
                }
                if let Ok((group, attempt, result)) =
                    done_rx.recv_timeout(Duration::from_millis(250))
                {
                    active -= 1;
                    match result {
                        Ok(saved) => {
                            for entry in &group {
                                downloaded.insert(entry.id.clone(), saved.path.clone());
                                deferred.remove(&entry.id);
                                let mut t = entry.clone();
                                t.path = saved.path.clone();
                                library.add(&t);
                            }
                            library.add(&saved);
                            completed += 1;
                            self.status.lock().unwrap_or_else(|e| e.into_inner()).saved += 1;
                            if completed % 5 == 0 || checkpoint.elapsed() >= Duration::from_secs(30)
                            {
                                save_json(&self.data.join(".youtube_downloads.json"), &downloaded)?;
                                checkpoint = Instant::now();
                            }
                        }
                        Err(error) => {
                            if !self.stopped() {
                                if attempt == 1 {
                                    queue.push_back((group, 2));
                                } else {
                                    for entry in &group {
                                        deferred.insert(
                                            entry.id.clone(),
                                            Deferred {
                                                title: entry.title.clone(),
                                                retry_after: now() + DOWNLOAD_COOLDOWN as f64,
                                            },
                                        );
                                    }
                                    save_json(
                                        &self.data.join(".youtube_deferred_downloads.json"),
                                        &deferred,
                                    )?;
                                    let mut s =
                                        self.status.lock().unwrap_or_else(|e| e.into_inner());
                                    s.deferred += 1;
                                    s.total = s.total.map(|n| n.saturating_sub(1));
                                }
                                let _ = error;
                            }
                        }
                    }
                }
            }
            save_json(&self.data.join(".youtube_downloads.json"), &downloaded)?;
            save_json(
                &self.data.join(".youtube_deferred_downloads.json"),
                &deferred,
            )?;
            self.check()?;
            {
                let mut s = self.status.lock().unwrap_or_else(|e| e.into_inner());
                s.ready = true;
                s.message = format!("{} songs ready · {completed} new songs saved", s.saved);
                let skipped = s.skipped;
                let deferred = s.deferred;
                if skipped > 0 {
                    s.message
                        .push_str(&format!(" · {} invalid or unavailable skipped", skipped));
                }
                if deferred > 0 {
                    s.message
                        .push_str(&format!(" · {} deferred for up to 30 days", deferred));
                }
            }
            self.status(None, Some("connected"));
            emit(&self.events, "ready", json!({}));
            Ok(())
        })();
        self.status.lock().unwrap_or_else(|e| e.into_inner()).busy = false;
        if let Err(error) = &result {
            self.status(
                Some(&if self.stopped() {
                    "Sync stopped. Your saved songs are kept. Resume setup to start listening."
                        .into()
                } else {
                    error.clone()
                }),
                Some("error"),
            );
        } else {
            self.status(None, None);
        }
        result
    }
}

pub struct YouTubeService {
    commands: Option<SyncSender<Value>>,
    events: Receiver<ServiceEvent>,
    backend: Arc<Backend>,
    workers: Vec<thread::JoinHandle<()>>,
    request: u64,
}
impl YouTubeService {
    pub fn spawn(root: &Path) -> Result<Self, String> {
        Self::spawn_with_state(root, &root.join("state"))
    }
    pub fn spawn_with_state(root: &Path, data: &Path) -> Result<Self, String> {
        fs::create_dir_all(data).map_err(|e| e.to_string())?;
        let cache = Arc::new(ProtectedCache {
            path: data.join("youtube_session.bin"),
            memory: Mutex::new(None),
            remembered: AtomicBool::new(true),
        });
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        let pipe = RustyPipe::builder()
            .storage(Box::new(CacheBackend(cache.clone())))
            .storage_dir(data)
            .no_reporter()
            .no_botguard()
            .timeout(Duration::from_secs(15))
            .build()
            .map_err(|e| e.to_string())?;
        let http = reqwest::blocking::Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(30))
            .user_agent("MusicApp-native/0.1")
            .build()
            .map_err(|e| e.to_string())?;
        let (tx, events) = mpsc::sync_channel(256);
        let (command_tx, commands) = mpsc::sync_channel::<Value>(16);
        let commands = Arc::new(Mutex::new(commands));
        let backend = Arc::new(Backend {
            root: root.into(),
            data: data.into(),
            pipe,
            runtime,
            http,
            stream_http: reqwest::Client::builder()
                .connect_timeout(Duration::from_secs(3))
                .read_timeout(Duration::from_secs(20))
                .build()
                .map_err(|e| e.to_string())?,
            cancel: AtomicBool::new(false),
            closed: AtomicBool::new(false),
            authentication_ui_ready: AtomicBool::new(false),
            status: Mutex::new(Status {
                message: "Connect YouTube to start listening.".into(),
                phase: "idle".into(),
                ..Default::default()
            }),
            cache,
            events: tx,
            active_path: Mutex::new(None),
            directories: Mutex::new(read_json(&data.join(".native_directories.json"))),
            resources: Mutex::new(ResourceMonitor::new()),
            slots: Mutex::new(Slots {
                active: 0,
                next: Instant::now(),
            }),
            metadata: Mutex::new(()),
            sync_owner: Mutex::new(()),
            video_locks: Mutex::new(HashMap::new()),
        });
        let mut workers = Vec::new();
        for _ in 0..2 {
            let backend = backend.clone();
            let commands = commands.clone();
            workers.push(thread::spawn(move||{loop{if backend.closed.load(Ordering::Relaxed){return;}let command=commands.lock().unwrap_or_else(|e|e.into_inner()).recv_timeout(Duration::from_millis(100));let Ok(command)=command else{continue};let action=command["action"].as_str().unwrap_or("");let id=command["request_id"].as_u64().unwrap_or(0);let result:Result<Value,String>=match action{
   "sync"=>backend.sync(&command).map(|_|Value::Null),"search"=>backend.search(command["query"].as_str().unwrap_or(""),command["limit"].as_u64().unwrap_or(5)as usize).and_then(|tracks|serde_json::to_value(tracks).map_err(|e|e.to_string())),
   "download"=>{let url=command["url"].as_str().unwrap_or("");let track=Track{id:super::youtube_video_id(url).unwrap_or_default(),url:Some(url.into()),..Default::default()};backend.download(&track,command["permanent"].as_bool().unwrap_or(false),command["directory"].as_str().map(Path::new)).and_then(|track|serde_json::to_value(track).map_err(|e|e.to_string()))},
   "forget_login"=>{let result=backend.call(backend.pipe.user_auth_remove_cookie());*backend.cache.memory.lock().unwrap_or_else(|e|e.into_inner())=None;let _=fs::remove_file(&backend.cache.path);let profile=browser_login::forget_owned_profile(&backend.data);backend.status.lock().unwrap_or_else(|e|e.into_inner()).connected=false;backend.status(None,None);result.and(profile).map(|_|Value::Null)},
   "options"=>save_json(&backend.data.join(".youtube_sync.json"),&json!({"source":command["source"],"host_radio":command["host_radio"],"auto_gaming":command["auto_gaming"]})).map(|_|Value::Null),"status"=>{backend.status(None,None);Ok(Value::Null)},"scan"=>{let dirs=command["directories"].as_array().map(|v|v.iter().filter_map(|s|s.as_str().map(str::to_string)).collect::<Vec<_>>()).unwrap_or_default();serde_json::to_value(super::scan_library_cached(&dirs,&backend.data.join(".sync_metadata.json"))).map_err(|e|e.to_string())},_=>Err("Unsupported native service operation".into())};
   if action!="sync"{match result{Ok(value)=>emit(&backend.events,"result",json!({"request_id":id,"action":action,"result":value})),Err(message)=>emit(&backend.events,"error",json!({"request_id":id,"action":action,"message":message,"code":"operation_failed"}))}}}
  }));
        }
        backend.status(None, None);
        Ok(Self {
            commands: Some(command_tx),
            events,
            backend,
            workers,
            request: 0,
        })
    }
    pub fn send(&mut self, mut command: Value) -> Result<u64, String> {
        self.request += 1;
        command["request_id"] = self.request.into();
        if matches!(command["action"].as_str(), Some("search" | "download"))
            && !self
                .backend
                .status
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .busy
        {
            self.backend.cancel.store(false, Ordering::Relaxed);
        }
        match command["action"].as_str().unwrap_or("") {
            "cancel" => self.backend.cancel.store(true, Ordering::Relaxed),
            "authentication_ui_ready" => self
                .backend
                .authentication_ui_ready
                .store(true, Ordering::Release),
            "active_path" => {
                *self
                    .backend
                    .active_path
                    .lock()
                    .unwrap_or_else(|e| e.into_inner()) =
                    command["path"].as_str().map(str::to_string)
            }
            "shutdown" => self.backend.closed.store(true, Ordering::Relaxed),
            _ => self
                .commands
                .as_ref()
                .ok_or("Service is closed")?
                .try_send(command)
                .map_err(|_| "YouTube request queue is busy")?,
        }
        Ok(self.request)
    }
    pub fn poll(&self) -> Option<ServiceEvent> {
        self.events.try_recv().ok()
    }
    pub fn recv_timeout(&self, timeout: Duration) -> Option<ServiceEvent> {
        self.events.recv_timeout(timeout).ok()
    }
    pub fn start_sync(
        &mut self,
        source: &str,
        login: bool,
        directories: &[String],
        playlists: &[String],
    ) -> Result<u64, String> {
        *self
            .backend
            .directories
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = directories.to_vec();
        save_json(
            &self.backend.data.join(".native_directories.json"),
            &directories,
        )?;
        self.send(json!({"action":"sync","source":source,"login":login,"directories":directories,"playlists":playlists}))
    }
    pub fn search(&mut self, query: &str, limit: usize) -> Result<u64, String> {
        self.send(json!({"action":"search","query":query,"limit":limit.clamp(1,20)}))
    }
    pub fn scan(&mut self, directories: &[String]) -> Result<u64, String> {
        self.send(json!({"action":"scan","directories":directories}))
    }
    pub fn download(
        &mut self,
        url: &str,
        permanent: bool,
        directory: Option<&Path>,
    ) -> Result<u64, String> {
        self.send(json!({"action":"download","url":url,"permanent":permanent,"directory":directory.map(|p|p.to_string_lossy())}))
    }
    pub fn cancel(&mut self) -> Result<u64, String> {
        self.send(json!({"action":"cancel"}))
    }
    pub fn forget_login(&mut self) -> Result<u64, String> {
        self.backend.cancel.store(false, Ordering::Relaxed);
        self.send(json!({"action":"forget_login"}))
    }
    pub fn active_path(&mut self, path: Option<&str>) -> Result<u64, String> {
        self.send(json!({"action":"active_path","path":path}))
    }
    /// Import only an explicitly supplied DPAPI-protected source session. Cookie
    /// values stay in memory and encrypted storage and are never returned.
    pub fn import_cookie_session(&mut self, protected: &[u8]) -> Result<Value, String> {
        self.backend.cancel.store(false, Ordering::Relaxed);
        let plain = browser_login::protect(protected, true).map_err(|_| {
            "Windows could not decrypt the existing MusicApp session for this account"
        })?;
        let cookies: Value = serde_json::from_slice(&plain)
            .map_err(|_| "The existing MusicApp session is damaged")?;
        let cookies = cookies
            .as_array()
            .ok_or("The existing session has an unsupported format")?;
        // Match the original Music API's name->value mapping, retaining the
        // final value for a repeated name while preserving first-name order.
        let mut pairs: Vec<(String, String)> = Vec::new();
        let mut original_count = 0usize;
        for cookie in cookies {
            let Some(name) = cookie["name"].as_str() else {
                continue;
            };
            let Some(value) = cookie["value"].as_str() else {
                continue;
            };
            let domain = cookie["domain"]
                .as_str()
                .unwrap_or("")
                .trim_start_matches('.')
                .to_ascii_lowercase();
            if domain != "youtube.com" && !domain.ends_with(".youtube.com") {
                continue;
            }
            if name.contains(['\r', '\n', ';', '=']) || value.contains(['\r', '\n']) {
                return Err("The existing session contains invalid cookie data".into());
            }
            original_count += 1;
            if let Some((_, previous)) = pairs.iter_mut().find(|(old, _)| old == name) {
                *previous = value.into();
            } else {
                pairs.push((name.into(), value.into()));
            }
        }
        if !pairs.iter().any(|(name, value)| {
            matches!(
                name.as_str(),
                "SAPISID" | "__Secure-3PAPISID" | "__Secure-1PAPISID"
            ) && !value.is_empty()
        }) {
            return Err("The existing session has no YouTube authentication cookie".into());
        }
        let header = pairs
            .iter()
            .map(|(name, value)| format!("{name}={value}"))
            .collect::<Vec<_>>()
            .join("; ");
        self.import_cookie_header(header, pairs.len(), original_count)
    }

    /// User-clicked extension handoff from the existing Chrome/Edge profile.
    pub fn connect_browser_profile(&mut self) -> Result<Value, String> {
        crate::browser_bridge::connect(|cookies| {
            let plain = serde_json::to_vec(&cookies).map_err(|_| "Invalid browser session")?;
            let protected = browser_login::protect(&plain, false)?;
            self.import_cookie_session(&protected)
        })
    }

    fn import_cookie_header(
        &mut self,
        header: String,
        count: usize,
        original_count: usize,
    ) -> Result<Value, String> {
        self.backend.cancel.store(false, Ordering::Relaxed);
        self.backend
            .call(self.backend.pipe.user_auth_set_cookie(header.clone()))
            .map_err(|_| {
                "YouTube could not verify the imported session; no sign-in browser was opened"
            })?;
        self.backend
            .call(self.backend.pipe.user_auth_check_cookie())
            .map_err(|_| {
                "YouTube could not verify the imported session; no sign-in browser was opened"
            })?;
        let cookie_values_match = self
            .backend
            .cache
            .memory
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
            .and_then(|text| serde_json::from_str::<Value>(text).ok())
            .is_some_and(|value| value["auth_cookie"]["cookie"].as_str() == Some(header.as_str()));
        if !cookie_values_match {
            return Err("The native session did not preserve the imported cookie values".into());
        }
        self.backend
            .status
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .connected = true;
        Ok(
            json!({"imported":true,"verified":true,"cookie_count":count,"source_cookie_count":original_count,"cookie_values_match":true,"remembered":self.backend.cache.remembered.load(Ordering::Relaxed)}),
        )
    }

    /// Verify the stored account with an HTTP read only. Never opens a browser
    /// or repairs/replaces a failed saved login.
    pub fn verify_session(&mut self) -> Result<Value, String> {
        self.backend.cancel.store(false, Ordering::Relaxed);
        self.backend
            .call(self.backend.pipe.user_auth_check_cookie())
            .map_err(|_| {
                "YouTube could not verify the saved session; no sign-in browser was opened"
            })?;
        Ok(
            json!({"verified":true,"remembered":self.backend.cache.remembered.load(Ordering::Relaxed)}),
        )
    }

    pub fn close(&mut self) {
        self.backend.closed.store(true, Ordering::Relaxed);
        self.backend.cancel.store(true, Ordering::Relaxed);
        self.commands.take();
        let deadline = Instant::now() + Duration::from_secs(1);
        while self.workers.iter().any(|w| !w.is_finished()) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        for worker in self.workers.drain(..) {
            if worker.is_finished() {
                let _ = worker.join();
            }
        }
    }
}
impl Drop for YouTubeService {
    fn drop(&mut self) {
        self.close();
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn browser_waits_for_hidden_ui_ack_and_restores_after_failure() {
        let (tx, rx) = mpsc::sync_channel(4);
        let ready = Arc::new(AtomicBool::new(false));
        let entered = Arc::new(AtomicBool::new(false));
        let worker_ready = ready.clone();
        let worker_entered = entered.clone();
        let worker = thread::spawn(move || {
            with_browser_ui(
                &tx,
                &worker_ready,
                &AtomicBool::new(false),
                &AtomicBool::new(false),
                Duration::from_secs(2),
                || {
                    worker_entered.store(true, Ordering::Relaxed);
                    Err::<(), _>("Synthetic sign-in failure".into())
                },
            )
        });
        let event = rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(event.event, "browser_auth");
        assert_eq!(event.get("active"), true);
        assert!(!entered.load(Ordering::Relaxed));
        ready.store(true, Ordering::Release);
        assert!(worker.join().unwrap().is_err());
        assert!(entered.load(Ordering::Relaxed));
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(1))
                .unwrap()
                .get("active"),
            false
        );
    }
    #[test]
    fn browser_never_launches_after_close_cancel_or_missing_ui_ack() {
        for (cancelled, closed) in [(true, false), (false, true), (false, false)] {
            let (tx, rx) = mpsc::sync_channel(4);
            let entered = AtomicBool::new(false);
            let result = with_browser_ui(
                &tx,
                &AtomicBool::new(false),
                &AtomicBool::new(cancelled),
                &AtomicBool::new(closed),
                Duration::from_millis(10),
                || {
                    entered.store(true, Ordering::Relaxed);
                    Ok(())
                },
            );
            assert!(result.is_err());
            assert!(!entered.load(Ordering::Relaxed));
            assert_eq!(rx.recv().unwrap().get("active"), true);
            assert_eq!(rx.recv().unwrap().get("active"), false);
        }
    }
    #[test]
    fn cuts_are_validated_sorted_and_merged() {
        assert_eq!(
            merge_cuts(vec![
                (5., 7.),
                (1., 3.),
                (2., 6.),
                (f64::NAN, 8.),
                (-1., 0.)
            ]),
            vec![(1., 7.)]
        );
    }
    #[test]
    fn duplicates_keep_upload_aliases_and_versions() {
        let make = |id: &str, title: &str| Track {
            id: id.into(),
            title: title.into(),
            artist: "Artist".into(),
            ..Default::default()
        };
        let groups = groups(vec![
            make("abcdefghijk", "Song"),
            make("lmnopqrstuv", "Song (Official Audio)"),
            make("ABCDEFGHIJK", "Song (Live)"),
        ]);
        assert_eq!(groups.len(), 2);
        assert_eq!(groups[0].len(), 2);
    }
    #[test]
    fn filename_is_windows_safe() {
        assert_eq!(safe_filename("A:B/C?*"), "A_B_C__");
    }
}
