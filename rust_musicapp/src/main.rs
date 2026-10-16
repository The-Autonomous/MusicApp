#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod audio;
mod browser_bridge;
mod lyrics;
mod migration;
mod model;
mod platform;
mod radio;
mod services;
mod tk;
use audio::{AudioEngine, ISO_BANDS};
use rand::seq::SliceRandom;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use services::{Track, YouTubeService};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Sender},
        Arc,
    },
    time::{Duration, Instant},
};
#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
struct Preferences {
    path: Option<String>,
    elapsed: f64,
    paused: bool,
    repeat: bool,
    volume: f32,
    gaming_mode: bool,
    accept_radio_eq: bool,
    current_radio_ip: String,
    youtube_download_permanently: bool,
    do_youtube_search: bool,
    visible: bool,
    player_enabled: bool,
    position: Option<(i32, i32)>,
    source: String,
    host_radio: bool,
    auto_gaming: bool,
    lyrics_visible: bool,
}
impl Default for Preferences {
    fn default() -> Self {
        Self {
            path: None,
            elapsed: 0.,
            paused: false,
            repeat: false,
            volume: 0.5,
            gaming_mode: true,
            accept_radio_eq: false,
            current_radio_ip: "0.0.0.0".into(),
            youtube_download_permanently: false,
            do_youtube_search: true,
            visible: true,
            player_enabled: true,
            position: None,
            source: services::DEFAULT_SOURCE.into(),
            host_radio: false,
            auto_gaming: true,
            lyrics_visible: true,
        }
    }
}
struct App {
    root: PathBuf,
    data: PathBuf,
    ui: tk::Tk,
    audio: AudioEngine,
    tx: Sender<Vec<String>>,
    prefs: Preferences,
    bindings: Vec<model::Binding>,
    pressed: HashSet<String>,
    latched: bool,
    edit: Option<String>,
    library: Vec<Track>,
    shuffler: model::Shuffler,
    current: Option<Track>,
    history: Vec<String>,
    history_index: usize,
    forward: Vec<String>,
    song_id: u64,
    lyric_lines: Vec<(f64, String)>,
    lyric: Option<lyrics::Lyrics>,
    service: Option<YouTubeService>,
    service_busy: bool,
    service_remembered: bool,
    service_idle: Option<Instant>,
    pending: HashMap<u64, String>,
    search_rows: Vec<(String, String, String)>,
    search_generation: u64,
    ready: bool,
    authentication_active: bool,
    overlay_permitted: bool,
    closing: bool,
    restart: bool,
    no_network: bool,
    directories: Vec<String>,
    playlists: Vec<String>,
    host: Option<radio::Host>,
    radio_client: Option<radio::Client>,
    scan: Option<radio::Scan>,
    stations: Vec<String>,
    radio_mode: bool,
    local_before_radio: Option<(Track, f64, bool)>,
    local_effects: Option<([f32; 10], f32)>,
    radio_download: Option<radio::Download>,
    radio_url: String,
    failed: HashMap<String, Instant>,
    radio_track: String,
    radio_received: Instant,
    radio_drift: Instant,
    drag: Option<(i32, i32, i32, i32)>,
    drag_monitors: Vec<platform::Rect>,
    last_top: Instant,
    last_monitor: Instant,
    last_save: Instant,
    last_display: Instant,
    last_progress: Instant,
    game: bool,
    dirty: bool,
    recommend: model::Recommendations,
    played_logged: bool,
    benchmark: Option<String>,
    started: Instant,
    seconds: Option<f64>,
    mute: bool,
}
fn numeric_handle(s: &str) -> isize {
    if let Some(v) = s.strip_prefix("0x") {
        isize::from_str_radix(v, 16).unwrap_or(0)
    } else {
        s.parse().unwrap_or(0)
    }
}
fn boolstr(v: bool) -> &'static str {
    if v {
        "1"
    } else {
        "0"
    }
}
fn default_dirs() -> Vec<String> {
    let home = PathBuf::from(std::env::var_os("USERPROFILE").unwrap_or_default());
    let mut result = Vec::new();
    for docs in [home.join("Documents"), home.join("OneDrive/Documents")] {
        for game in ["GTAV Enhanced", "GTA V"] {
            let p = docs.join("Rockstar Games").join(game).join("User Music");
            if p.is_dir() {
                result.push(p.to_string_lossy().into());
            }
        }
    }
    if let Some(docs) = platform::documents() {
        for game in ["GTA V", "GTAV Enhanced"] {
            let p = docs.join("Rockstar Games").join(game).join("User Music");
            if p.is_dir() {
                let p = p.to_string_lossy().into_owned();
                if !result.contains(&p) {
                    result.insert(0, p);
                }
            }
        }
    }
    let old = home.join("MusicPlayerYoutubeDownloads");
    if old.is_dir() {
        result.push(old.to_string_lossy().into());
    }
    result
}
impl App {
    fn restore_local_effects(&mut self) {
        if let Some((gains, volume)) = self.local_effects.take() {
            for (f, g) in ISO_BANDS.into_iter().zip(gains) {
                self.audio.set_eq_gain(f, g);
            }
            self.audio.set_volume(if self.mute { 0. } else { volume });
        }
    }
    fn leave_radio(&mut self) {
        self.radio_mode = false;
        self.radio_client.take();
        self.scan.take();
        self.audio.stop();
        self.radio_download.take();
        self.radio_url.clear();
        self.restore_local_effects();
        if let Some((track, pos, paused)) = self.local_before_radio.take() {
            self.prefs.paused = paused;
            self.play(track, pos, false);
        } else {
            self.current = None;
            self.next();
        }
    }
    fn local_search(&self, query: &str, limit: usize) -> Vec<Track> {
        let query = query.to_lowercase();
        static SPLIT: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(||regex::Regex::new(r"[\s.,_-]+").unwrap());
        let split = &*SPLIT;
        let stop = ["by", "the", "a", "an", "in", "on", "ft", "feat", "and", "&"];
        let words: Vec<_> = split
            .split(query.trim())
            .filter(|s| !s.is_empty() && !stop.contains(s))
            .collect();
        if words.is_empty() {
            return Vec::new();
        }
        let mut matches: Vec<_> = self
            .library
            .iter()
            .enumerate()
            .filter_map(|(i, t)| {
                let clean = format!("{} {}", t.artist, t.title)
                    .to_lowercase()
                    .chars()
                    .filter(|c| c.is_alphanumeric() || c.is_whitespace() || *c == '_')
                    .collect::<String>();
                words.iter().all(|w| clean.contains(w)).then_some((
                    clean.chars().count().abs_diff(query.chars().count()),
                    i,
                    t.clone(),
                ))
            })
            .collect();
        matches.sort_by_key(|(len, i, _)| (*len, *i));
        matches.into_iter().take(limit).map(|(_, _, t)| t).collect()
    }
    fn call(&self, name: &str, args: &[&str]) {
        if let Err(e) = self.ui.call(name, args) {
            self.log(&e);
        }
    }
    fn log(&self, text: &str) {
        if let Ok(mut f) = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.data.join("musicapp.log"))
        {
            let _ = writeln!(f, "{:.3} {text}", radio::now());
        }
    }
    fn error(&self, message: &str) {
        self.log(message);
        self.call(
            "::music::youtube_state",
            &["error", message, "0", boolstr(self.ready), "0", "0", ""],
        );
    }
    fn overlay_handle(&self) -> isize {
        self.ui
            .eval("if {[winfo exists .overlay]} {winfo id .overlay} else {set _h 0}")
            .ok()
            .map(|s| numeric_handle(&s))
            .unwrap_or(0)
    }
    fn save(&mut self) {
        if !self.radio_mode {
            self.prefs.elapsed = self.audio.position();
            if let Some(t) = &self.current {
                self.prefs.path = Some(t.path.clone());
            }
            self.prefs.paused = self.audio.is_paused();
        } else if let Some((t, p, paused)) = &self.local_before_radio {
            self.prefs.path = Some(t.path.clone());
            self.prefs.elapsed = *p;
            self.prefs.paused = *paused;
        }
        let _ = model::save(&self.data.join("preferences.json"), &self.prefs);
        let _ = model::save(
            &self.data.join(".player_recommend_data.json"),
            &self.recommend,
        );
        self.last_save = Instant::now();
        self.dirty = false;
    }
    fn binding_ui(&self, message: &str) {
        for b in &self.bindings {
            let keys = b
                .required
                .iter()
                .map(|s| tk::word(s))
                .collect::<Vec<_>>()
                .join(" ");
            self.call(
                "::music::binding_update",
                &[&b.id, &keys, boolstr(b.alt_needed), message],
            );
        }
    }
    fn get_service(&mut self) -> Result<&mut YouTubeService, String> {
        if self.no_network {
            return Err("Network is disabled for this diagnostic run".into());
        }
        if self.service.is_none() {
            let mut service=YouTubeService::spawn_with_state(&self.root,&self.data)?;
            let _=service.active_path(self.current.as_ref().map(|track|track.path.as_str()));
            self.service=Some(service);
        }
        self.service_idle = None;
        Ok(self.service.as_mut().unwrap())
    }
    fn start_sync(&mut self) {
        if self.no_network {
            return;
        }
        self.service_busy = true;
        self.service_idle = None;
        let source = self.prefs.source.clone();
        let dirs = self.directories.clone();
        let lists = self.playlists.clone();
        match self
            .get_service()
            .and_then(|s| s.start_sync(&source, false, &dirs, &lists))
        {
            Ok(_) => {}
            Err(e) => self.error(&e),
        }
    }
    fn eq_ui(&self) {
        let gains = self.audio.eq_gains().map(|v| v.to_string()).join(" ");
        let echo = self.audio.echo().unwrap_or(audio::EchoParams {
            delay_ms: 350.,
            feedback: 0.35,
            wet: 0.,
        });
        self.call(
            "::music::eq_state",
            &[
                &gains,
                &(self.prefs.volume * 100.).to_string(),
                &echo.delay_ms.to_string(),
                &(echo.wet * 100.).to_string(),
                boolstr(self.audio.gaming()),
                boolstr(self.prefs.accept_radio_eq),
            ],
        );
    }
    fn options_ui(&self) {
        self.call(
            "::music::options_state",
            &[
                &self.prefs.source,
                boolstr(self.prefs.host_radio),
                boolstr(self.prefs.auto_gaming),
            ],
        );
    }
    fn title(&self) -> String {
        if self.benchmark.is_some() {
            return if self
                .benchmark
                .as_deref()
                .is_some_and(|s| s.starts_with("paused"))
            {
                "Benchmark Artist - Benchmark Track -[Paused]-"
            } else {
                "Benchmark Artist - Benchmark Track"
            }
            .into();
        }
        let Some(t) = &self.current else {
            return String::new();
        };
        let mut title = if t.artist == "Unknown Artist" || t.title.contains(&t.artist) {
            t.title.clone()
        } else {
            format!("{} - {}", t.artist, t.title)
        };
        if self.prefs.repeat {
            title.push_str("*+*");
        }
        if self.audio.is_paused() {
            title.push_str("*=*");
        }
        model::clean_title(&title)
    }
    fn update_overlay_visibility(&mut self) {
        let available = !self.authentication_active && self.prefs.visible
            && (self.benchmark.is_some() || model::overlay_available(
                self.ready, self.prefs.player_enabled, self.current.is_some(),
                self.audio.is_playing() && !self.audio.finished(), self.audio.is_paused(), self.authentication_active));
        if available != self.overlay_permitted {
            self.overlay_permitted = available;
            self.call("::music::overlay_available", &[boolstr(available)]);
        }
    }
    fn display(&mut self) {
        self.update_overlay_visibility();
        let pos = self.audio.position();
        let duration = self
            .current
            .as_ref()
            .and_then(|t| t.duration)
            .unwrap_or(self.audio.duration());
        let lyric = if self.prefs.lyrics_visible {
            self.lyric_lines
                .iter()
                .rev()
                .find(|(t, _)| *t <= pos)
                .map(|(_, s)| s.as_str())
                .unwrap_or("")
        } else {
            ""
        };
        let title = self.title();
        self.call(
            "::music::set_display",
            &[&title, &pos.to_string(), &duration.to_string(), lyric],
        );
        if !self.radio_mode {
            if let Some(host) = &self.host {
                let mut s = host.status.lock().unwrap_or_else(|e| e.into_inner());
                s.title = title;
                s.path = self
                    .current
                    .as_ref()
                    .map(|t| t.path.clone())
                    .unwrap_or_default();
                s.track_id = self.song_id.to_string();
                s.duration = duration;
                s.position = pos;
                s.paused = self.audio.is_paused() || self.current.is_none();
                s.repeat = self.prefs.repeat;
                s.volume = self.prefs.volume;
                s.eq = ISO_BANDS.into_iter().zip(self.audio.eq_gains()).collect();
                s.lyrics = json!(self.lyric_lines);
            }
        }
        if !self.played_logged && duration > 0. && pos >= duration / 2. && !platform::idle() {
            if let Some(t) = &self.current {
                self.recommend.played(t);
                self.dirty = true;
            }
            self.played_logged = true;
        }
        self.last_display = Instant::now();
    }
    fn play(&mut self, track: Track, start: f64, record: bool) {
        if !self.ready || !self.prefs.player_enabled {
            return;
        }
        self.audio
            .set_gaming(self.prefs.gaming_mode || (self.prefs.auto_gaming && self.game));
        match self.audio.play_with_duration(&track.path, start, track.duration) {
            Ok(()) => {
                self.current = Some(track.clone());
                self.song_id += 1;
                self.played_logged = false;
                self.lyric_lines.clear();
                if record
                    && !Path::new(&track.path)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .starts_with(".youtubeCached")
                {
                    self.history.truncate(if self.history.is_empty() {
                        0
                    } else {
                        self.history_index + 1
                    });
                    self.history.push(track.path.clone());
                    if self.history.len() > 50 {
                        self.history.remove(0);
                    }
                    self.history_index = self.history.len().saturating_sub(1);
                    self.shuffler.record(&track);
                }
                if self.prefs.paused {
                    self.audio.pause();
                }
                if !self.no_network {
                    if self.lyric.is_none() {
                        self.lyric = Some(lyrics::Lyrics::new(self.data.clone(), self.tx.clone()));
                    }
                    self.lyric
                        .as_ref()
                        .unwrap()
                        .request(self.song_id, &track.artist, &track.title);
                }
                if let Some(s) = &mut self.service {
                    let _ = s.active_path(Some(&track.path));
                }
                self.dirty = true;
                self.display();
            }
            Err(e) => {
                self.log(&format!("Cannot play {}: {e}", track.path));
                self.call(
                    "::music::set_display",
                    &["Cannot play", "0", "0", &track.title],
                );
                self.current = None;
                self.prefs.path = None;
                self.failed
                    .insert(track.path, Instant::now() + Duration::from_secs(60));
            }
        }
    }
    fn next(&mut self) {
        if !self.ready {
            return;
        }
        if let Some(path) = self.forward.pop() {
            if let Some(t) = self.library.iter().find(|t| t.path == path).cloned() {
                self.history_index =
                    (self.history_index + 1).min(self.history.len().saturating_sub(1));
                self.play(t, 0., false);
                return;
            }
        }
        for _ in 0..self.library.len() {
            if let Some(t) = self.shuffler.choose(&self.library) {
                if self
                    .failed
                    .get(&t.path)
                    .is_none_or(|until| Instant::now() > *until)
                {
                    self.play(t, 0., true);
                    break;
                }
            } else {
                break;
            }
        }
    }
    fn previous(&mut self) {
        if !self.ready {
            return;
        }
        if self.audio.position() > 3. && self.forward.is_empty() {
            if let Some(t) = self.current.clone() {
                self.play(t, 0., false);
            }
            return;
        }
        if self.history_index > 0 {
            if let Some(t) = &self.current {
                self.forward.push(t.path.clone());
            }
            self.history_index -= 1;
            let path = &self.history[self.history_index];
            if let Some(t) = self.library.iter().find(|t| &t.path == path).cloned() {
                self.play(t, 0., false);
            }
        } else if let Some(t) = self.current.clone() {
            self.play(t, 0., false);
        }
    }
    fn search(&mut self, query: &str, use_youtube: bool, dont_log: bool) {
        if !dont_log {
            self.recommend.search(query);
            self.dirty = true;
        }
        self.search_generation += 1;
        self.search_rows.clear();
        let tracks = self.local_search(query, 100);
        let n = tracks.len();
        for t in tracks {
            self.search_rows
                .push((format!("{} - {}", t.artist, t.title), t.path, "path".into()));
        }
        if n > 5 {
            self.search_rows
                .insert(0, (query.into(), query.into(), "playlist".into()));
        }
        self.publish_results();
        if use_youtube && !query.trim().is_empty() {
            match self.get_service().and_then(|s| s.search(query, 5)) {
                Ok(id) => {
                    self.pending
                        .insert(id, format!("search:{}", self.search_generation));
                }
                Err(e) => self.log(&e),
            }
        }
    }
    fn publish_results(&self) {
        let rows = self
            .search_rows
            .iter()
            .map(|(t, p, k)| format!("{} {} {}", tk::word(t), tk::word(p), tk::word(k)))
            .map(|row| tk::word(&row))
            .collect::<Vec<_>>()
            .join(" ");
        self.call("::music::results", &[&rows]);
    }
    fn add_track(&mut self, t: Track) {
        if Path::new(&t.path)
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .starts_with(".youtubeCached")
        {
            return;
        }
        if let Some(old) = self
            .library
            .iter_mut()
            .find(|v| v.path.eq_ignore_ascii_case(&t.path))
        {
            *old = t;
        } else {
            self.library.push(t);
            self.shuffler.refill(self.library.len());
        }
        self.host_library();
    }
    fn host_library(&self) {
        if let Some(h) = &self.host {
            h.status.lock().unwrap_or_else(|e| e.into_inner()).library = self
                .library
                .iter()
                .map(|t| (format!("{} - {}", t.artist, t.title), t.path.clone()))
                .collect();
        }
    }
    fn hosting(&mut self, on: bool) {
        if on && self.host.is_none() {
            match radio::Host::start(self.root.clone(), self.tx.clone()) {
                Ok(h) => {
                    self.log(&format!(
                        "Phone controls: http://{}:8080/app_pad",
                        radio::local_ip()
                    ));
                    self.host = Some(h);
                    self.host_library();
                }
                Err(e) => {
                    self.log(&e);
                    self.error(&e);
                    self.prefs.host_radio = false;
                }
            }
        } else if !on {
            self.host.take();
        }
    }
    fn radio_connect(&mut self, ip: &str) {
        self.radio_client.take();
        self.audio.stop();
        self.radio_download.take();
        match radio::Client::connect(ip, self.tx.clone()) {
            Ok(client) => {
                self.radio_client = Some(client);
                self.prefs.current_radio_ip = ip.into();
                self.radio_track.clear();
            }
            Err(e) => self.log(&e),
        }
    }
    fn handle(&mut self, a: Vec<String>) {
        if a.is_empty() {
            return;
        }
        let action = a[0].as_str();
        if self.authentication_active && matches!(action, "key" | "mouse") { return; }
        let arg = |i: usize| a.get(i).map(String::as_str).unwrap_or("");
        let number = |i| arg(i).parse::<f32>().unwrap_or(0.);
        match action {
            "tick" => {}
            "hooks_reset" => {
                self.pressed.clear();
                self.latched = false;
            }
            "ui_error" => self.log(&format!("UI error: {}", arg(1))),
            "key" => {
                let key = arg(2);
                if arg(1) == "up" {
                    self.pressed.remove(key);
                    self.latched = false;
                    return;
                }
                if self.edit.is_some() {
                    if key == "escape" {
                        self.edit = None;
                        self.call("::music::controls_escape", &[]);
                        return;
                    }
                    if ["alt", "right alt", "ctrl"].contains(&key) {
                        return;
                    }
                    let id = self.edit.clone().unwrap();
                    match model::remap(&mut self.bindings, &id, key) {
                        Ok(()) => {
                            self.edit = None;
                            let _ = model::save_bindings(
                                &self.data.join(".keyBindings.json"),
                                &self.bindings,
                            );
                            self.binding_ui("Shortcut updated.");
                        }
                        Err(e) => self.binding_ui(&e),
                    }
                    return;
                }
                if self.pressed.contains(key) {
                    return;
                }
                for name in ["alt", "right alt", "shift", "right shift", "ctrl"] {
                    if platform::held(name) {
                        self.pressed.insert(name.into());
                    } else {
                        self.pressed.remove(name);
                    }
                }
                self.pressed.insert(key.into());
                let media = match key {
                    "media_next" => Some("skip_next"),
                    "media_previous" => Some("skip_previous"),
                    "media_play_pause" => Some("pause_play"),
                    _ => None,
                };
                if let Some(id) = media {
                    self.handle(vec![id.into()]);
                } else if !self.latched {
                    if let Some(id) =
                        model::triggered(&self.bindings, &self.pressed).map(str::to_string)
                    {
                        self.latched = true;
                        self.handle(vec![id]);
                    }
                }
            }
            "mouse" => {
                let (x, y) = (number(2) as i32, number(3) as i32);
                let h = self.overlay_handle();
                if h == 0 {
                    return;
                }
                let r = platform::bounds(h);
                match arg(1) {
                    "down" => {
                        if self.overlay_permitted
                            && self.ui.eval("expr {[winfo exists .overlay] && [winfo ismapped .overlay]}").ok().as_deref() == Some("1")
                            && x >= r.left
                            && x < r.right
                            && y >= r.top
                            && y < r.bottom
                        {
                            self.drag = Some((x, y, r.left, r.top));
                            self.drag_monitors = platform::monitors();
                            platform::set_drag_tracking(true);
                            platform::style_overlay(h, false);
                            self.call("::music::overlay_drag_begin", &[]);
                        }
                    }
                    "move" => {
                        if let Some((sx, sy, wx, wy)) = self.drag {
                            let (x, y) = platform::clamp(
                                wx + x - sx,
                                wy + y - sy,
                                r.right - r.left,
                                r.bottom - r.top,
                                &self.drag_monitors,
                            );
                            self.call("::music::set_position", &[&x.to_string(), &y.to_string()]);
                            self.prefs.position = Some((x, y));
                        }
                    }
                    "up" => {
                        if self.drag.take().is_some() {
                            platform::set_drag_tracking(false);
                            platform::style_overlay(h, true);
                            let r = platform::bounds(h);
                            self.prefs.position = Some((r.left, r.top));
                            self.call(
                                "::music::set_position",
                                &[&r.left.to_string(), &r.top.to_string()],
                            );
                            self.dirty = true;
                            self.call("::music::overlay_drag_end", &[]);
                        }
                    }
                    _ => {}
                }
            }
            "overlay_style" | "search_style" => {
                let h = numeric_handle(arg(1));
                if h != 0 {
                    platform::style_overlay(h, arg(2) != "0");
                }
            }
            "overlay_position" => {
                self.prefs.position = Some((number(1) as i32, number(2) as i32));
                self.dirty = true;
            }
            "toggle_overlay" => {
                self.prefs.visible = !self.prefs.visible;
                self.update_overlay_visibility();
                self.dirty = true;
            }
            "player_on_off" => {
                self.prefs.player_enabled = !self.prefs.player_enabled;
                if !self.prefs.player_enabled {
                    if self.radio_mode {
                        self.leave_radio();
                    }
                    self.audio.pause();
                    self.call("::music::hide_overlay", &[]);
                    let _ = self
                        .ui
                        .eval("if {[winfo exists .controls]} {destroy .controls}");
                } else {
                    if self.ready {
                        if self.current.is_some() {
                            self.audio.resume();
                        } else {
                            self.next();
                        }
                    }
                }
                self.dirty = true;
            }
            "show_hints" => {
                self.call("::music::controls", &[]);
            }
            "show_search" => {
                if !self.radio_mode {
                    self.call("::music::search", &[]);
                }
            }
            "show_eq_menu" => {
                self.call("::music::eq", &[]);
            }
            "youtube_sync" => {
                self.call("::music::youtube", &[]);
            }
            "skip_next" => {
                if !self.radio_mode && self.prefs.player_enabled {
                    self.prefs.paused = false;
                    self.next();
                }
            }
            "skip_previous" => {
                if !self.radio_mode && self.prefs.player_enabled {
                    self.prefs.paused = false;
                    self.previous();
                }
            }
            "pause_play" => {
                if self.ready && self.prefs.player_enabled {
                    if self.radio_mode {
                        self.handle(vec!["radio_enable_toggle".into()]);
                    } else {
                        self.audio.toggle_pause();
                        self.prefs.paused = self.audio.is_paused();
                        self.dirty = true;
                    }
                }
            }
            "force_pause" => {
                self.audio.pause();
                self.prefs.paused = true;
                self.dirty = true;
            }
            "force_play" => {
                if self.ready && self.prefs.player_enabled {
                    self.audio.resume();
                    self.prefs.paused = false;
                    self.dirty = true;
                }
            }
            "volume_up" | "volume_down" => {
                self.prefs.volume = (self.prefs.volume
                    + if action == "volume_up" { 0.05 } else { -0.05 })
                .clamp(0., 1.);
                self.audio
                    .set_volume(if self.mute { 0. } else { self.prefs.volume });
                self.dirty = true;
                self.eq_ui();
            }
            "repeat_toggle" => {
                self.prefs.repeat = !self.prefs.repeat;
                self.dirty = true;
            }
            "lyrics_toggle_visibility" => {
                self.prefs.lyrics_visible = !self.prefs.lyrics_visible;
                let _ = self.ui.eval(&format!(
                    "set ::music::lyrics_visible {}",
                    boolstr(self.prefs.lyrics_visible)
                ));
                self.dirty = true;
            }
            "binding_edit" => {
                self.edit = Some(arg(1).into());
            }
            "binding_cancel" => {
                self.edit = None;
            }
            "binding_reset" => {
                self.bindings = model::defaults();
                let _ = model::save_bindings(&self.data.join(".keyBindings.json"), &self.bindings);
                self.binding_ui("Default shortcuts restored.");
            }
            "binding_alt" => {
                if let Some(pos) = self.bindings.iter().position(|b| b.id == arg(1)) {
                    let mut candidate = self.bindings[pos].clone();
                    candidate.alt_needed = self
                        .ui
                        .eval(&format!("set ::music::alt_required({})", candidate.id))
                        .unwrap_or_default()
                        == "1";
                    if self.bindings.iter().any(|b| {
                        b.id != candidate.id && model::chord(b) == model::chord(&candidate)
                    }) {
                        self.binding_ui("That shortcut is already in use.");
                    } else {
                        self.bindings[pos] = candidate;
                        let _ = model::save_bindings(
                            &self.data.join(".keyBindings.json"),
                            &self.bindings,
                        );
                        self.binding_ui("");
                    }
                }
            }
            "search" => self.search(arg(1), arg(2) == "1", arg(3) == "1"),
            "search_recommendation" => {
                let s = self.recommend.random_suggestion();
                if !s.is_empty() {
                    self.call("::music::search_submit", &[&s, "1"]);
                }
            }
            "search_youtube" => {
                self.prefs.do_youtube_search = arg(1) == "1";
                self.dirty = true;
            }
            "download_permanent" => {
                self.prefs.youtube_download_permanently = arg(1) == "1";
                self.dirty = true;
            }
            "search_select" | "remote_play" => {
                if !self.ready {
                    return;
                }
                let path = arg(1);
                let kind = if action == "remote_play" {
                    "path"
                } else {
                    arg(2)
                };
                if kind == "path" {
                    if let Some(track) = self
                        .library
                        .iter()
                        .find(|t| t.path == path)
                        .cloned()
                        .or_else(|| services::track_from_path(path))
                    {
                        self.forward.clear();
                        self.shuffler.replay.clear();
                        self.prefs.paused = false;
                        self.play(track, 0., true);
                        self.call("::music::close_search", &[]);
                    }
                } else if kind == "url" {
                    let url = path.to_string();
                    let permanent = self.prefs.youtube_download_permanently;
                    match self
                        .get_service()
                        .and_then(|s| s.download(&url, permanent, None))
                    {
                        Ok(id) => {
                            self.pending.insert(id, "download".into());
                        }
                        Err(e) => self.error(&e),
                    }
                } else if kind == "playlist" {
                    let mut tracks = self.local_search(path, 250);
                    tracks.shuffle(&mut rand::thread_rng());
                    if let Some(first) = tracks.first().cloned() {
                        self.forward.clear();
                        self.shuffler.replay =
                            tracks.iter().skip(1).map(|t| t.path.clone()).collect();
                        self.prefs.paused = false;
                        self.play(first, 0., true);
                        self.call("::music::close_search", &[]);
                    }
                }
            }
            "eq_request" => self.eq_ui(),
            "eq_band" => {
                self.audio.set_eq_gain(number(1) as u32, number(2));
            }
            "eq_preset" => {
                for (i, f) in ISO_BANDS.iter().enumerate() {
                    self.audio.set_eq_gain(*f, number(i + 2));
                }
            }
            "echo_delay" | "echo_wet" => {
                let mut echo = self.audio.echo().unwrap_or(audio::EchoParams {
                    delay_ms: self.ui.eval("set ::music::delay").ok().and_then(|s|s.parse::<f32>().ok()).map(|v|v*10.).unwrap_or(350.),
                    feedback: 0.35,
                    wet: 0.,
                });
                if action == "echo_delay" {
                    echo.delay_ms = number(1);
                } else {
                    echo.wet = number(1) / 100.;
                }
                if echo.wet <= 0. {
                    self.audio.disable_echo();
                } else {
                    self.audio.set_echo(echo.delay_ms, echo.feedback, echo.wet);
                }
            }
            "volume" => {
                self.prefs.volume = (number(1) / 100.).clamp(0., 1.);
                self.audio
                    .set_volume(if self.mute { 0. } else { self.prefs.volume });
                self.dirty = true;
            }
            "gaming" => {
                self.prefs.gaming_mode = arg(1) == "1";
                self.audio
                    .set_gaming(self.prefs.gaming_mode || (self.prefs.auto_gaming && self.game));
                self.eq_ui();
                self.dirty = true;
            }
            "accept_radio_eq" => {
                self.prefs.accept_radio_eq = arg(1) == "1";
                if !self.prefs.accept_radio_eq {
                    self.restore_local_effects();
                }
                self.dirty = true;
            }
            "youtube_start" | "sync_playlists" => self.start_sync(),
            "youtube_cancel" => {
                if let Some(s) = &mut self.service {
                    let _ = s.cancel();
                }
            }
            "youtube_forget" => {
                if let Err(e) = self.get_service().and_then(|s| s.forget_login()) {
                    self.error(&e);
                }
            }
            "options_request" => self.options_ui(),
            "options_save" => {
                self.prefs.source = arg(1).into();
                self.prefs.host_radio = arg(2) == "1";
                self.prefs.auto_gaming = arg(3) == "1";
                self.hosting(self.prefs.host_radio);
                let settings = json!({"source":self.prefs.source,"host_radio":self.prefs.host_radio,"auto_gaming":self.prefs.auto_gaming});
                let _ = model::save(&self.data.join(".youtube_sync.json"), &settings);
                if let Some(s) = &mut self.service {
                    let _=s.send(json!({"action":"options","source":self.prefs.source,"host_radio":self.prefs.host_radio,"auto_gaming":self.prefs.auto_gaming}));
                }
                self.dirty = true;
            }
            "library" => {
                if let Ok(tracks) = serde_json::from_str::<Vec<Track>>(arg(1)) {
                    for track in tracks {
                        if !self
                            .library
                            .iter()
                            .any(|t| t.path.eq_ignore_ascii_case(&track.path))
                        {
                            self.library.push(track);
                        }
                    }
                    self.shuffler.refill(self.library.len());
                    self.host_library();
                    if self.ready && self.current.is_none() {
                        let resume = self
                            .prefs
                            .path
                            .as_ref()
                            .and_then(|p| self.library.iter().find(|t| &t.path == p))
                            .cloned();
                        if let Some(t) = resume {
                            self.play(t, self.prefs.elapsed, true);
                        } else {
                            self.next();
                        }
                    }
                }
            }
            "lyrics_result" => {
                if arg(1).parse::<u64>().ok() == Some(self.song_id) {
                    self.lyric_lines = serde_json::from_str(arg(2)).unwrap_or_default();
                }
            }
            "game" => {
                self.game = arg(1) == "true";
                self.audio
                    .set_gaming(self.prefs.gaming_mode || (self.prefs.auto_gaming && self.game));
            }
            "radio_enable_toggle" => {
                if !self.ready {
                    return;
                }
                self.radio_mode = !self.radio_mode;
                if self.radio_mode {
                    self.local_before_radio = self
                        .current
                        .clone()
                        .map(|t| (t, self.audio.position(), self.audio.is_paused()));
                    self.audio.stop();
                    let ip = self.prefs.current_radio_ip.clone();
                    if ip != "0.0.0.0" {
                        self.radio_connect(&ip);
                    } else {
                        self.handle(vec!["radio_scan_station".into()]);
                    }
                } else {
                    self.leave_radio();
                }
            }
            "radio_scan_station" => {
                if self.no_network {
                    return;
                }
                if self.stations.len() > 1 {
                    let n = self
                        .stations
                        .iter()
                        .position(|s| s == &self.prefs.current_radio_ip)
                        .unwrap_or(0);
                    let ip = self.stations[(n + 1) % self.stations.len()].clone();
                    self.radio_connect(&ip);
                } else if self.scan.is_none() {
                    self.scan = Some(radio::Scan::start(self.tx.clone()));
                }
            }
            "radio_found" => {
                if !self.stations.contains(&arg(1).to_string()) {
                    self.stations.push(arg(1).into());
                }
                if self.radio_mode && self.radio_client.is_none() {
                    self.radio_connect(arg(1));
                }
            }
            "radio_status" => {
                if !self.radio_mode || !self.prefs.player_enabled {
                    return;
                }
                if let Ok(data) = serde_json::from_str::<Value>(arg(1)) {
                    let id = data["track_id"].as_str().unwrap_or("");
                    let pos = data["location"].as_f64().unwrap_or(0.)
                        + data["latency"].as_f64().unwrap_or(0.);
                    let paused = data["paused"].as_bool().unwrap_or(false);
                    if id != self.radio_track
                        || (!self.audio.is_playing()
                            && !paused
                            && self.radio_download.is_none()
                            && pos < data["duration"].as_f64().unwrap_or(0.) - 1.)
                    {
                        if id != self.radio_track {
                            self.audio.stop();
                            self.radio_download.take();
                        }
                        let url = data["url"].as_str().unwrap_or("");
                        if let Ok(parsed) = url::Url::parse(url) {
                            if parsed.host_str() == Some(self.prefs.current_radio_ip.as_str())
                                && parsed.scheme() == "http"
                            {
                                self.radio_url = url.into();
                                if self.audio.play_with_duration(url, pos, data["duration"].as_f64()).is_ok() {
                                    self.current = Some(Track {
                                        path: url.into(),
                                        title: data["title"].as_str().unwrap_or("Radio").into(),
                                        artist: String::new(),
                                        duration: data["duration"].as_f64(),
                                        ..Default::default()
                                    });
                                    self.radio_track = id.into();
                                    self.lyric_lines.clear();
                                    self.song_id += 1;
                                }
                            }
                        }
                    } else if !paused
                        && (self.audio.position() - pos).abs() > 1.5
                        && self.radio_drift.elapsed() > Duration::from_secs(30)
                    {
                        let _ = self.audio.seek(pos);
                        self.radio_drift = Instant::now();
                    }
                    if paused {
                        self.audio.pause();
                    } else {
                        self.audio.resume();
                    }
                    self.prefs.repeat = data["repeat"].as_bool().unwrap_or(false);
                    if self.prefs.accept_radio_eq {
                        if self.local_effects.is_none() {
                            self.local_effects = Some((self.audio.eq_gains(), self.audio.volume()));
                        }
                        self.audio.set_volume(if self.mute {
                            0.
                        } else {
                            data["volume"].as_f64().unwrap_or(0.5).clamp(0., 1.) as f32
                        });
                        for pair in data["eq"].as_str().unwrap_or("").split(',') {
                            if let Some((f, g)) = pair.split_once(':') {
                                if let (Ok(f), Ok(g)) = (f.parse(), g.parse()) {
                                    self.audio.set_eq_gain(f, g);
                                }
                            }
                        }
                    }
                    self.radio_received = Instant::now();
                }
            }
            "radio_lyrics" => {
                if self.radio_mode && arg(2) == self.radio_track {
                    self.lyric_lines = serde_json::from_str(arg(1)).unwrap_or_default();
                }
            }
            "radio_download" => {
                if self.radio_mode && arg(2) == self.radio_track {
                    let pos = self.audio.position();
                    if self.audio.play_with_duration(arg(1), pos, self.current.as_ref().and_then(|track|track.duration)).is_ok() {
                        self.radio_url.clear();
                    }
                }
            }
            "radio_error" => self.log(arg(1)),
            "player_restart" => {
                self.restart = true;
                self.closing = true;
            }
            "kill_self" => self.closing = true,
            "status" => {}
            _ => self.log(&format!("Unknown action: {action}")),
        }
    }
    fn service_events(&mut self) {
        loop {
            let event = self.service.as_ref().and_then(|s| s.poll());
            let Some(e) = event else { break };
            match e.event.as_str() {
                "browser_auth" => {
                    self.authentication_active = e.get("active").as_bool().unwrap_or(false);
                    self.pressed.clear();
                    self.latched = false;
                    if self.drag.take().is_some() {
                        platform::set_drag_tracking(false);
                        let h = self.overlay_handle();
                        if h != 0 { platform::style_overlay(h, true); }
                    }
                    if self.authentication_active { self.update_overlay_visibility(); }
                    let hidden = self.ui.call("::music::authentication_ui", &[boolstr(self.authentication_active)]).is_ok();
                    if !self.authentication_active { self.update_overlay_visibility(); }
                    if self.authentication_active && hidden {
                        if let Some(service) = &mut self.service {
                            let _ = service.send(json!({"action":"authentication_ui_ready"}));
                        }
                    }
                }
                "ready" => {
                    self.ready = true;
                    self.service_idle = Some(Instant::now());
                    if self.current.is_none() {
                        let resume = self
                            .prefs
                            .path
                            .as_ref()
                            .and_then(|p| self.library.iter().find(|t| &t.path == p))
                            .cloned();
                        if let Some(t) = resume {
                            self.play(t, self.prefs.elapsed, true);
                        } else {
                            self.next();
                        }
                    }
                }
                "status" => {
                    let phase = e.get("phase").as_str().unwrap_or("idle");
                    let message = e.get("message").as_str().unwrap_or("");
                    let total = e
                        .get("total")
                        .as_u64()
                        .map(|v| v.to_string())
                        .unwrap_or_default();
                    let saved = e.get("saved").as_u64().unwrap_or(0).to_string();
                    let connected = e.get("connected").as_bool().unwrap_or(false);
                    let ready = e.get("ready").as_bool().unwrap_or(false);
                    let busy = e.get("busy").as_bool().unwrap_or(false);
                    self.service_busy = busy;
                    self.service_remembered = e.get("remembered").as_bool().unwrap_or(true);
                    self.call(
                        "::music::youtube_state",
                        &[
                            phase,
                            message,
                            boolstr(connected),
                            boolstr(ready),
                            boolstr(busy),
                            &saved,
                            &total,
                            boolstr(e.get("remembered").as_bool().unwrap_or(true)),
                        ],
                    );
                    if let Some(rows) = e.get("downloads").as_array() {
                        let list = rows
                            .iter()
                            .take(3)
                            .map(|r| {
                                let fraction = r["progress"]
                                    .as_f64()
                                    .map(|v| v.to_string())
                                    .unwrap_or_default();
                                let title = r["title"].as_str().unwrap_or("Downloading…");
                                let phase = r["phase"].as_str().unwrap_or("");
                                let status = if phase == "processing" {
                                    match r["processor"].as_str().unwrap_or("") {
                                        "ModifyChapters" => "Removing non-music sections".into(),
                                        "SponsorBlock" => "Checking SponsorBlock".into(),
                                        _ => "Converting / saving".into(),
                                    }
                                } else {
                                    r["progress"]
                                        .as_f64()
                                        .map(|v| format!("{:.0}%", v * 100.))
                                        .unwrap_or_else(|| "Preparing download".into())
                                };
                                tk::word(&format!(
                                    "{} {}",
                                    tk::word(&format!("{title} {status}")),
                                    tk::word(&fraction)
                                ))
                            })
                            .collect::<Vec<_>>()
                            .join(" ");
                        self.call("::music::youtube_downloads", &[&list]);
                    }
                    if !busy {
                        self.service_idle = Some(Instant::now());
                    } else {
                        self.service_idle = None;
                    }
                }
                "track" => {
                    if let Ok(t) = serde_json::from_value::<Track>(e.get("track").clone()) {
                        self.add_track(t);
                    }
                }
                "result" => {
                    let id = e.get("request_id").as_u64().unwrap_or(0);
                    let pending = self.pending.remove(&id);
                    match pending.as_deref() {
                        Some("download") => {
                            if let Ok(t) = serde_json::from_value::<Track>(e.get("result").clone())
                            {
                                self.add_track(t.clone());
                                self.prefs.paused = false;
                                self.play(t, 0., true);
                                self.call("::music::close_search", &[]);
                            }
                        }
                        Some(s) if s == format!("search:{}", self.search_generation) => {
                            if let Some(rows) = e.get("result").as_array() {
                                for r in rows {
                                    let title = r["title"].as_str().unwrap_or("YouTube");
                                    let url = format!(
                                        "https://www.youtube.com/watch?v={}",
                                        r["id"].as_str().unwrap_or("")
                                    );
                                    self.search_rows.push((
                                        format!("{title} (OFFICIAL SONG)"),
                                        url,
                                        "url".into(),
                                    ));
                                }
                                self.publish_results();
                            }
                        }
                        _ => {}
                    }
                    self.service_idle = Some(Instant::now());
                }
                "error" => {
                    let id = e.get("request_id").as_u64().unwrap_or(0);
                    let op = self.pending.remove(&id);
                    let msg = e
                        .get("message")
                        .as_str()
                        .unwrap_or("Service operation failed");
                    self.log(msg);
                    if op.as_deref() == Some("download") {
                        self.call("::music::close_search", &[]);
                    }
                    if op.is_none() {
                        self.error(msg);
                    }
                    self.service_idle = Some(Instant::now());
                }
                _ => {}
            }
        }
    }
    fn tick(&mut self) {
        self.service_events();
        self.update_overlay_visibility();
        if self.benchmark.as_deref() == Some("progress")
            && self.last_progress.elapsed() >= Duration::from_millis(100)
        {
            self.last_progress = Instant::now();
            let count = (self.started.elapsed().as_millis() / 100) % 100;
            self.call(
                "::music::youtube_state",
                &[
                    "download",
                    "Benchmark: synthetic progress, no network",
                    "1",
                    "0",
                    "1",
                    &count.to_string(),
                    "100",
                    "1",
                ],
            );
            let list = (0..3)
                .map(|i| {
                    let f = ((count + i * 17) % 100) as f64 / 100.;
                    tk::word(&format!(
                        "{} {}",
                        tk::word(&format!("Benchmark song {} · {:.0}%", i + 1, f * 100.)),
                        f
                    ))
                })
                .collect::<Vec<_>>()
                .join(" ");
            self.call("::music::youtube_downloads", &[&list]);
        }
        if self.benchmark.is_none()
            && self.ready
            && self.prefs.player_enabled
            && !self.prefs.paused
            && !self.radio_mode
            && (self.current.is_none() || self.audio.finished())
        {
            if self.prefs.repeat && self.current.is_some() {
                if let Some(t) = self.current.clone() {
                    self.play(t, 0., false);
                }
            } else {
                self.next();
            }
        }
        if self.radio_mode
            && self.prefs.player_enabled
            && !self.radio_url.is_empty()
            && self.audio.finished()
            && self.radio_download.is_none()
        {
            match radio::Download::start(
                &self.radio_url,
                self.data.clone(),
                self.radio_track.clone(),
                self.tx.clone(),
            ) {
                Ok(d) => self.radio_download = Some(d),
                Err(e) => self.log(&e),
            }
        }
        if self.last_display.elapsed() >= Duration::from_millis(250) {
            self.display();
        }
        if self.drag.is_none() && self.last_monitor.elapsed() >= Duration::from_secs(1) {
            let h = self.overlay_handle();
            if h != 0 {
                let r = platform::bounds(h);
                let (x, y) = platform::clamp(
                    r.left,
                    r.top,
                    r.right - r.left,
                    r.bottom - r.top,
                    &platform::monitors(),
                );
                if (x, y) != (r.left, r.top) {
                    platform::position(h, x, y);
                    self.prefs.position = Some((x, y));
                    self.dirty = true;
                }
            }
            self.last_monitor = Instant::now();
        }
        if self.last_top.elapsed() >= Duration::from_secs(5) {
            let h = self.overlay_handle();
            if h != 0 {
                platform::topmost(h);
            }
            self.last_top = Instant::now();
        }
        if self.last_save.elapsed() >= Duration::from_secs(20)
            && (self.dirty || self.audio.is_playing())
        {
            self.save();
        }
        if !self.service_busy
            && self.service_remembered
            && self.pending.is_empty()
            && self
                .service_idle
                .is_some_and(|t| t.elapsed() > Duration::from_secs(5))
        {
            if let Some(s) = self.service.take() {
                std::thread::spawn(move || drop(s));
            }
            self.service_idle = None;
        }
        if self
            .seconds
            .is_some_and(|n| self.started.elapsed().as_secs_f64() >= n)
        {
            self.closing = true;
        }
    }
}
fn run() -> Result<(), String> {
    let launch = Instant::now();
    let mut startup_marks = Vec::new();
    let args: Vec<_> = std::env::args().collect();
    let opt = |name: &str| {
        args.iter()
            .position(|v| v == name)
            .and_then(|i| args.get(i + 1))
            .cloned()
    };
    let flag = |name: &str| args.iter().any(|v| v == name);
    if let Some(browser) = opt("--browser") {
        if !matches!(browser.as_str(), "chrome" | "edge") { return Err("Use --browser chrome or --browser edge".into()); }
        std::env::set_var("MUSICAPP_BROWSER", browser);
    }
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let root = opt("--root")
        .map(PathBuf::from)
        .or_else(|| {
            exe.ancestors()
                .find(|p| p.join("ui/app.tcl").is_file())
                .map(Path::to_path_buf)
        })
        .ok_or("Native UI resources are missing. Keep the runtime, tools and ui folders beside MusicApp.exe.")?;
    let data = opt("--state-dir")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("state"));
    fs::create_dir_all(&data).map_err(|e| e.to_string())?;
    std::env::set_var("MUSICAPP_FFMPEG", root.join("tools/ffmpeg.exe"));
    if flag("--connect-browser-profile") {
        let mut service = YouTubeService::spawn_with_state(&root, &data)?;
        let report = service.connect_browser_profile()?;
        model::save(&data.join("youtube-session-verification.json"), &report)?;
        return Ok(());
    }
    if let Some(source) = opt("--import-python-profile") {
        let report = migration::import_profile(Path::new(&source), &root, &data)?;
        model::save(&data.join("profile_import_report.json"), &report)?;
        println!("{}", report);
        return Ok(());
    }
    if flag("--verify-youtube-session") {
        let mut service = YouTubeService::spawn_with_state(&root, &data)?;
        let report = service.verify_session()?;
        model::save(&data.join("youtube-session-verification.json"), &report)?;
        println!("{}", report);
        return Ok(());
    }
    if flag("--diagnostics") || flag("--setup") {
        println!(
            "{}",
            json!({"version":env!("CARGO_PKG_VERSION"),"root":root,"state":data,"ffmpeg":root.join("tools/ffmpeg.exe").is_file(),"tcl":root.join("runtime/tcl86t.dll").is_file(),"settings_isolated":true})
        );
        return Ok(());
    }
    if flag("--update") {
        return Err("This native port has no configured binary update feed. Use build.bat to rebuild the native application; the original Python updater is never invoked.".into());
    }
    platform::dpi_aware();
    startup_marks.push(("paths_and_dpi", launch.elapsed().as_secs_f64()*1000.));
    let (tx, rx) = mpsc::channel();
    let ui = tk::Tk::new(&root, tx.clone())?;
    startup_marks.push(("native_tk", launch.elapsed().as_secs_f64()*1000.));
    ui.eval(&format!(
        "source -encoding utf-8 {}",
        tk::word(&root.join("ui/app.tcl").to_string_lossy())
    ))?;
    ui.eval(
        "proc ::rust_tick {} {rust_action tick; after 100 ::rust_tick}; after 100 ::rust_tick",
    )?;
    startup_marks.push(("ui_definitions", launch.elapsed().as_secs_f64()*1000.));
    let had_state = data.join("preferences.json").exists();
    let prefs: Preferences =
        serde_json::from_value(model::load(&data.join("preferences.json"))).unwrap_or_default();
    let no_network = flag("--no-network") || opt("--benchmark").is_some();
    let benchmark = opt("--benchmark");
    let bindings = model::load_bindings(&data.join(".keyBindings.json"));
    let audio = AudioEngine::new(data.clone());
    startup_marks.push(("profile_and_audio_config", launch.elapsed().as_secs_f64()*1000.));
    if had_state {
        audio.set_volume(prefs.volume);
    }
    if flag("--mute") {
        audio.set_volume(0.);
    }
    let stop = Arc::new(AtomicBool::new(false));
    let hooks = if flag("--no-hooks") || benchmark.is_some() {
        None
    } else {
        Some(platform::Hooks::start(tx.clone())?)
    };
    let game_thread = if benchmark.is_none() {
        Some(platform::start_game_monitor(tx.clone(), stop.clone()))
    } else {
        None
    };
    let mut directories = default_dirs();
    if let Some(dir) = opt("--library") {
        directories = vec![dir];
    }
    let playlists = fs::read_to_string(root.join("Playlists.txt"))
        .unwrap_or_default()
        .lines()
        .map(str::trim)
        .filter(|s| !s.is_empty() && !s.starts_with('#'))
        .map(str::to_string)
        .collect();
    startup_marks.push(("hooks_and_library_paths", launch.elapsed().as_secs_f64()*1000.));
    let mut app = App {
        root: root.clone(),
        data: data.clone(),
        ui,
        audio,
        tx: tx.clone(),
        prefs,
        bindings,
        pressed: HashSet::new(),
        latched: false,
        edit: None,
        library: Vec::new(),
        shuffler: model::Shuffler::new(),
        current: None,
        history: Vec::new(),
        history_index: 0,
        forward: Vec::new(),
        song_id: 0,
        lyric_lines: Vec::new(),
        lyric: None,
        service: None,
        service_busy: false,
        service_remembered: true,
        service_idle: None,
        pending: HashMap::new(),
        search_rows: Vec::new(),
        search_generation: 0,
        ready: no_network,
        authentication_active: false,
        overlay_permitted: false,
        closing: false,
        restart: false,
        no_network,
        directories,
        playlists,
        host: None,
        radio_client: None,
        scan: None,
        stations: Vec::new(),
        radio_mode: false,
        local_before_radio: None,
        local_effects: None,
        radio_download: None,
        radio_url: String::new(),
        failed: HashMap::new(),
        radio_track: String::new(),
        radio_received: Instant::now(),
        radio_drift: Instant::now(),
        drag: None,
        drag_monitors: Vec::new(),
        last_top: Instant::now(),
        last_monitor: Instant::now(),
        last_save: Instant::now(),
        last_display: Instant::now(),
        last_progress: Instant::now(),
        game: false,
        dirty: false,
        recommend: serde_json::from_value(model::load(&data.join(".player_recommend_data.json")))
            .unwrap_or_default(),
        played_logged: false,
        benchmark: benchmark.clone(),
        started: Instant::now(),
        seconds: opt("--seconds").and_then(|s| s.parse().ok()),
        mute: flag("--mute"),
    };
    app.audio.set_gaming(app.prefs.gaming_mode);
    app.binding_ui("");
    app.options_ui();
    let _ = app.ui.eval(&format!(
        "set ::music::lyrics_visible {}; set ::music::use_youtube {}; set ::music::permanent {}",
        boolstr(app.prefs.lyrics_visible),
        boolstr(app.prefs.do_youtube_search),
        boolstr(app.prefs.youtube_download_permanently)
    ));
    if let Some((x, y)) = app.prefs.position {
        app.call("::music::set_position", &[&x.to_string(), &y.to_string()]);
    }
    let _ = app.ui.eval(&format!("set ::music::native_mouse_hooks {}", boolstr(hooks.is_some())));
    app.update_overlay_visibility();
    if let Some(scenario) = &benchmark {
        // Each controlled case starts from the same effects state. Personal
        // settings are exercised separately by playback_profile.
        app.prefs.player_enabled = true;
        app.prefs.visible = true;
        app.call("::music::show_overlay", &[]);
        app.audio.disable_echo();
        if scenario != "playback_profile" {
            app.prefs.gaming_mode = true;
            app.audio.set_gaming(true);
            for f in ISO_BANDS {
                app.audio.set_eq_gain(f, 0.);
            }
        }
        // Initialize effects before opening playback in both implementations,
        // so launch-to-ready includes the same effect preparation workload.
        if scenario == "playback_eq" {
            app.prefs.gaming_mode = false;
            app.audio.set_gaming(false);
            for (f, g) in ISO_BANDS.into_iter().zip([3., -2., 6., -7., 2., -3., 10., -4., 5., 8.]) {
                app.audio.set_eq_gain(f, g);
            }
        } else if scenario == "playback_echo" {
            app.prefs.gaming_mode = false;
            app.audio.set_gaming(false);
            app.audio.enable_echo(350., 0.35, 0.5);
        }
        app.current = Some(Track {
            title: "Benchmark Track".into(),
            artist: "Benchmark Artist".into(),
            duration: Some(60.),
            ..Default::default()
        });
        if let Some(media) = opt("--media") {
            if scenario.starts_with("playback") || scenario.starts_with("paused") {
                let track = services::track_from_path(&media).unwrap_or(Track {
                    path: media,
                    ..Default::default()
                });
                app.library = vec![track.clone()];
                app.play(track, 0., false);
                if !app.audio.is_playing() {
                    return Err("Benchmark audio did not start; see state/musicapp.log".into());
                }
                if scenario.starts_with("paused") {
                    app.audio.pause();
                    if let Some(t) = &mut app.current {
                        t.duration = Some(60.);
                    }
                }
            }
        }
        match scenario.as_str() {
            "idle_hidden" | "paused_hidden" => {
                app.call("::music::hide_overlay", &[]);
                app.prefs.visible = false;
            }
            "controls" => app.call("::music::controls", &[]),
            "search" => {
                app.call("::music::search", &[]);
                app.search_rows = Vec::new();
                app.publish_results();
            }
            "progress" => {
                app.call("::music::youtube", &["0"]);
                app.call(
                    "::music::youtube_state",
                    &[
                        "download",
                        "Downloading your music",
                        "1",
                        "0",
                        "1",
                        "10",
                        "100",
                        "1",
                    ],
                );
                let rows = (0..3)
                    .map(|i| {
                        tk::word(&format!(
                            "{} 0.5",
                            tk::word(&format!("Track {i} · Downloading 50%"))
                        ))
                    })
                    .collect::<Vec<_>>()
                    .join(" ");
                app.call("::music::youtube_downloads", &[&rows]);
            }
            _ => {}
        }
        app.display();
        let _ = app.ui.eval("update idletasks");
        fs::write(data.join("ready"), b"ready").map_err(|e| e.to_string())?;
    } else {
        let dirs = app.directories.clone();
        let cache = data.join(".musicapp_meta.json");
        let library_tx = tx.clone();
        std::thread::spawn(move || {
            let tracks = services::scan_library_cached(&dirs, &cache);
            let _ = library_tx.send(vec![
                "library".into(),
                serde_json::to_string(&tracks).unwrap_or_default(),
            ]);
        });
        if app.prefs.host_radio {
            app.hosting(true);
        }
        if !no_network {
            app.call("::music::youtube", &["1"]);
        } else {
            fs::write(data.join("ready"), b"ready").map_err(|e| e.to_string())?;
        }
    }
    startup_marks.push(("ready", launch.elapsed().as_secs_f64()*1000.));
    if flag("--startup-profile") {
        let _ = model::save(&data.join("startup-profile.json"), &json!({"cumulative_milliseconds":startup_marks}));
    }
    while !app.closing {
        app.ui.event();
        let mut motion = None;
        for _ in 0..256 {
            match rx.try_recv() {
                Ok(a) if a.first().map(String::as_str) == Some("mouse") && a.get(1).map(String::as_str) == Some("move") => motion = Some(a),
                Ok(a) => {
                    if let Some(position) = motion.take() { app.handle(position); }
                    app.handle(a);
                },
                Err(_) => break,
            }
        }
        if let Some(position) = motion { app.handle(position); }
        app.tick();
    }
    let _ = model::save(
        &data.join("runtime-stats.json"),
        &json!({"underflows":app.audio.underflows(),"buffered_seconds":app.audio.buffered_seconds(),"audio_error":app.audio.last_error()}),
    );
    if app.benchmark.is_some() {
        let _ = model::save(
            &data.join("benchmark_audio.json"),
            &json!({"scenario":app.benchmark,"position_seconds":app.audio.position(),"duration_seconds":app.audio.duration(),"is_playing":app.audio.is_playing(),"is_paused":app.audio.is_paused(),"underflows":app.audio.underflows(),"last_error":app.audio.last_error(),"gaming_mode":app.audio.gaming(),"eq_gains":app.audio.eq_gains(),"echo":app.audio.echo().map(|p|json!({"delay_ms":p.delay_ms,"feedback":p.feedback,"wet":p.wet})),"volume":app.audio.volume()}),
        );
    }
    app.save();
    stop.store(true, Ordering::Relaxed);
    drop(hooks);
    app.audio.shutdown();
    app.scan.take();
    app.radio_client.take();
    app.host.take();
    app.service.take();
    app.lyric.take();
    if let Some(j) = game_thread {
        j.thread().unpark();
        let _ = j.join();
    }
    if app.restart {
        let mut cmd = std::process::Command::new(exe);
        cmd.args(args.iter().skip(1));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000);
        }
        cmd.spawn().map_err(|e| e.to_string())?;
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("{e}");
        if std::env::args().any(|s| matches!(s.as_str(), "--benchmark" | "--diagnostics" | "--import-python-profile" | "--verify-youtube-session")) {
            std::process::exit(1);
        }
        platform::message(&e);
        std::process::exit(1);
    }
}
