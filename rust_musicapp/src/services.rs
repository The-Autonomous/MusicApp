//! Native library identity/cache policy and on-demand YouTube services.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use unicode_normalization::UnicodeNormalization;

pub const AUDIO_SUFFIXES: &[&str] = &["mp3", "wav", "ogg", "flac", "m4a", "opus", "webm"];
pub const DOWNLOAD_COOLDOWN: u64 = 30 * 24 * 60 * 60;
pub const DEFAULT_SOURCE: &str = "YouTube Music liked songs";
pub const SOURCES: &[&str] = &[DEFAULT_SOURCE, "YouTube liked videos"];

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Track {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub path: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub artist: String,
    #[serde(default)]
    pub duration: Option<f64>,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub is_available: Option<bool>,
    #[serde(default)]
    pub availability: Option<String>,
}

/// Read tags and duration without starting the Python integration process.
pub fn track_from_path(path: impl AsRef<Path>) -> Option<Track> {
    use lofty::file::{AudioFile, TaggedFileExt};
    use lofty::tag::Accessor;
    let path = path.as_ref();
    if !audio_file_exists(path) {
        // Temporary playback files are deliberately hidden from library scans,
        // but their metadata is still needed when selected from YouTube.
        static CACHE_NAME: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
            regex::Regex::new(
                r"(?i)^\.youtubeCached \[[A-Za-z0-9_-]{11}\]\.(mp3|m4a|webm|opus|ogg|wav|flac)$",
            )
            .unwrap()
        });
        if !path
            .file_name()
            .is_some_and(|n| CACHE_NAME.is_match(&n.to_string_lossy()))
        {
            return None;
        }
        let meta = path.symlink_metadata().ok()?;
        use std::os::windows::fs::MetadataExt;
        if !meta.is_file()
            || meta.len() == 0
            || meta.file_attributes() & (0x400 | 0x1000 | 0x40000 | 0x400000) != 0
        {
            return None;
        }
    }
    let absolute = std::path::absolute(path).ok()?;
    let mut track = Track {
        path: absolute.to_string_lossy().into_owned(),
        title: path.file_stem()?.to_string_lossy().into_owned(),
        artist: "Unknown Artist".into(),
        ..Default::default()
    };
    if let Ok(tagged) = lofty::probe::Probe::open(path).and_then(|p| p.read()) {
        track.duration = Some(tagged.properties().duration().as_secs_f64());
        if let Some(tag) = tagged.primary_tag().or_else(|| tagged.first_tag()) {
            if let Some(title) = tag.title() {
                if !title.is_empty() {
                    track.title = title.into_owned();
                }
            }
            if let Some(artist) = tag.artist() {
                if !artist.is_empty() {
                    track.artist = artist.into_owned();
                }
            }
        }
    }
    if !track.duration.is_some_and(|v| v.is_finite() && v > 0.0) {
        // FFmpeg covers WebM/Matroska and downloaded containers unsupported by
        // Lofty. This reads headers only; it never decodes or alters the file.
        if let Some(binary) = metadata_ffmpeg() {
            let mut command = Command::new(binary);
            command
                .args(["-hide_banner", "-nostdin", "-i"])
                .arg(path)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::piped());
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                command.creation_flags(0x08000000 | 0x00004000);
            }
            if let Ok(output) = command.output() {
                let message = String::from_utf8_lossy(&output.stderr);
                let duration =
                    regex::Regex::new(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)").unwrap();
                if let Some(parts) = duration.captures(&message) {
                    let get = |i| {
                        parts
                            .get(i)
                            .and_then(|s| s.as_str().parse::<f64>().ok())
                            .unwrap_or(0.0)
                    };
                    track.duration = Some(get(1) * 3600.0 + get(2) * 60.0 + get(3));
                }
                for line in message.lines() {
                    if let Some((key, value)) = line.trim().split_once(':') {
                        match key.trim().to_ascii_lowercase().as_str() {
                            "title" if !value.trim().is_empty() => {
                                track.title = value.trim().into()
                            }
                            "artist" if !value.trim().is_empty() => {
                                track.artist = value.trim().into()
                            }
                            _ => (),
                        }
                    }
                }
            }
        }
    }
    track
        .duration
        .is_some_and(|v| v.is_finite() && v > 0.0)
        .then_some(track)
}

fn metadata_ffmpeg() -> Option<PathBuf> {
    static BINARY: std::sync::OnceLock<Option<PathBuf>> = std::sync::OnceLock::new();
    BINARY.get_or_init(metadata_ffmpeg_uncached).clone()
}
fn metadata_ffmpeg_uncached() -> Option<PathBuf> {
    fn find(root: &Path, depth: usize) -> Option<PathBuf> {
        if root.is_file() {
            return Some(root.to_path_buf());
        }
        if depth == 0 {
            return None;
        }
        for entry in fs::read_dir(root).ok()?.flatten() {
            let path = entry.path();
            if entry.file_type().ok()?.is_file()
                && entry
                    .file_name()
                    .to_string_lossy()
                    .to_ascii_lowercase()
                    .starts_with("ffmpeg")
                && path.extension().is_some_and(|e| e == "exe")
            {
                return Some(path);
            }
            if entry.file_type().ok()?.is_dir() {
                if let Some(binary) = find(&path, depth - 1) {
                    return Some(binary);
                }
            }
        }
        None
    }
    if let Some(root) = std::env::var_os("MUSICAPP_FFMPEG") {
        if let Some(binary) = find(Path::new(&root), 4) {
            return Some(binary);
        }
    }
    let mut roots = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        for root in exe.ancestors().skip(1).take(4) {
            roots.push(root.to_path_buf());
        }
    }
    if let Ok(root) = std::env::current_dir() {
        roots.push(root.clone());
        if let Some(parent) = root.parent() {
            roots.push(parent.to_path_buf());
        }
    }
    for root in roots {
        for name in ["runtime/ffmpeg.exe", "ffmpeg.exe", "ffmpeg-bin", "tools"] {
            if let Some(binary) = find(&root.join(name), 4) {
                return Some(binary);
            }
        }
    }
    None
}

#[derive(Serialize, Deserialize)]
struct CachedTrack {
    mtime: u128,
    size: u64,
    #[serde(flatten)]
    track: Track,
}
fn scan_paths(directories: &[String]) -> Vec<PathBuf> {
    let mut pending: Vec<PathBuf> = directories
        .iter()
        .filter(|p| !p.contains("://"))
        .map(PathBuf::from)
        .collect();
    let mut seen = HashSet::new();
    let mut paths = Vec::new();
    while let Some(directory) = pending.pop() {
        if !seen.insert(path_key(&directory)) {
            continue;
        }
        let Ok(entries) = fs::read_dir(directory) else {
            continue;
        };
        for entry in entries.flatten() {
            if entry.file_name().to_string_lossy().starts_with('.') {
                continue;
            }
            let Ok(kind) = entry.file_type() else {
                continue;
            };
            if kind.is_dir() {
                pending.push(entry.path());
            } else if kind.is_file() && audio_file_exists(entry.path()) {
                paths.push(entry.path());
            }
        }
    }
    paths.sort();
    paths.dedup_by(|a, b| path_key(a) == path_key(b));
    paths
}

pub fn scan_library(directories: &[String]) -> Vec<Track> {
    scan_paths(directories)
        .into_iter()
        .filter_map(track_from_path)
        .collect()
}

pub fn scan_library_cached(directories: &[String], cache_path: &Path) -> Vec<Track> {
    let mut cache: HashMap<String, CachedTrack> = fs::read(cache_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default();
    let mut updated = HashMap::new();
    let mut result = Vec::new();
    for path in scan_paths(directories) {
        let Ok(meta) = path.metadata() else {
            continue;
        };
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let key = std::path::absolute(&path)
            .unwrap_or_else(|_| path.clone())
            .to_string_lossy()
            .into_owned();
        let track = match cache.remove(&key) {
            Some(entry) if entry.mtime == mtime && entry.size == meta.len() => Some(entry.track),
            _ => track_from_path(&path),
        };
        if let Some(track) = track {
            if track.duration.is_some_and(|d| d.is_finite() && d > 0.0) {
                updated.insert(
                    key,
                    CachedTrack {
                        mtime,
                        size: meta.len(),
                        track: track.clone(),
                    },
                );
                result.push(track);
            }
        }
    }
    if let Ok(bytes) = serde_json::to_vec(&updated) {
        if let Some(parent) = cache_path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let temporary = cache_path.with_extension("json.tmp");
        if fs::write(&temporary, bytes).is_ok() {
            let _ = fs::rename(temporary, cache_path);
        }
    }
    result
}

pub fn audio_file_exists(path: impl AsRef<Path>) -> bool {
    let path = path.as_ref();
    if path
        .file_name()
        .is_none_or(|n| n.to_string_lossy().starts_with('.'))
        || !path.extension().is_some_and(|ext| {
            AUDIO_SUFFIXES.contains(&ext.to_string_lossy().to_lowercase().as_str())
        })
    {
        return false;
    }
    let Ok(meta) = path.metadata() else {
        return false;
    };
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & (0x1000 | 0x40000 | 0x400000) != 0 {
            return false;
        }
    }
    meta.is_file() && meta.len() > 0
}

fn valid_id(id: &str) -> bool {
    id.len() == 11
        && id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

pub fn youtube_video_id(value: &str) -> Option<String> {
    if value.len() > 4096 {
        return None;
    }
    let parsed = url::Url::parse(value.trim()).ok()?;
    if !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.port().is_some_and(|p| p != 80 && p != 443)
    {
        return None;
    }
    let host = parsed.host_str()?;
    let parts: Vec<_> = parsed.path().trim_matches('/').split('/').collect();
    let youtube = matches!(
        host,
        "youtube.com" | "www.youtube.com" | "music.youtube.com" | "m.youtube.com"
    );
    let candidate = if matches!(host, "youtu.be" | "www.youtu.be") && parts.len() == 1 {
        parts[0].to_owned()
    } else if youtube && parsed.path().trim_end_matches('/') == "/watch" {
        let values: Vec<_> = parsed
            .query_pairs()
            .filter(|(k, v)| k == "v" && !v.is_empty())
            .map(|(_, v)| v.into_owned())
            .collect();
        if values.len() != 1 {
            return None;
        }
        values[0].clone()
    } else if parts.len() == 2
        && ((youtube && matches!(parts[0], "shorts" | "embed" | "live"))
            || (matches!(host, "youtube-nocookie.com" | "www.youtube-nocookie.com")
                && parts[0] == "embed"))
    {
        parts[1].to_owned()
    } else {
        return None;
    };
    valid_id(&candidate).then_some(candidate)
}

pub fn valid_playlist_url(value: &str) -> bool {
    let Ok(url) = url::Url::parse(value) else {
        return false;
    };
    let lists: Vec<_> = url
        .query_pairs()
        .filter(|(k, v)| k == "list" && !v.is_empty())
        .map(|(_, v)| v)
        .collect();
    matches!(url.scheme(), "http" | "https")
        && url.username().is_empty()
        && url.password().is_none()
        && !url.port().is_some_and(|p| p != 80 && p != 443)
        && matches!(
            url.host_str(),
            Some("youtube.com" | "www.youtube.com" | "music.youtube.com" | "m.youtube.com")
        )
        && matches!(url.path().trim_end_matches('/'), "/playlist" | "/watch")
        && lists.len() == 1
        && !lists[0].is_empty()
        && lists[0]
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

fn normalized(text: &str) -> String {
    // NFKC plus case folding of the common multi-character distinction. The
    // frozen helper remains the authority for the download queue's exact keys.
    text.nfkc()
        .flat_map(char::to_lowercase)
        .collect::<String>()
        .replace('ß', "ss")
        .replace('ς', "σ")
        .trim()
        .into()
}
fn artist_key(value: &str) -> String {
    let value = normalized(value);
    static TOPIC: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"\s*-\s*topic$").unwrap());
    let topic = &*TOPIC;
    topic
        .replace(&value, "")
        .chars()
        .filter(|c| c.is_alphanumeric())
        .collect()
}
fn title_key(value: &str) -> String {
    let mut value = normalized(value);
    static PROMOTION: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"(?i)\s*[\[(](?:official(?: music)? (?:video|audio)|music video|audio|lyric(?:s)?(?: video)?|official lyric(?:s)?(?: video)?|visuali[sz]er)[\])]\s*$").unwrap()
    });
    let promotion = &*PROMOTION;
    loop {
        let cleaned = promotion.replace(&value, "").into_owned();
        if cleaned == value {
            break;
        }
        value = cleaned;
    }
    value = value.replace('&', " and ").replace(['\'', '’'], "");
    value
        .chars()
        .map(|c| if c.is_alphanumeric() { c } else { ' ' })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}
fn known(value: &str) -> bool {
    !matches!(
        value,
        "" | "youtube" | "unknownartist" | "unknown" | "untitled"
    )
}

pub fn song_keys(track: &Track) -> BTreeSet<(String, String)> {
    let artist = artist_key(&track.artist);
    static SPLITTER: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"\s+[-–—]\s+").unwrap());
    let splitter = &*SPLITTER;
    let parts: Vec<_> = splitter.splitn(&track.title, 2).collect();
    let mut title = track.title.as_str();
    let mut pairs = BTreeSet::new();
    if parts.len() == 2 {
        let embedded = artist_key(parts[0]);
        if embedded == artist {
            title = parts[1];
        } else if !known(&artist) {
            pairs.insert((embedded, title_key(parts[1])));
        }
    }
    pairs.insert((artist, title_key(title)));
    pairs.retain(|(a, t)| known(a) && !t.is_empty() && known(&t.replace(' ', "")));
    pairs
}

pub fn eligible_tracks(tracks: Vec<Track>) -> (Vec<Track>, usize) {
    let mut result = Vec::new();
    let mut seen = HashSet::new();
    let mut skipped = 0;
    for track in tracks {
        if !valid_id(&track.id)
            || track.url.as_ref().is_some_and(|u| {
                !u.is_empty()
                    && u != &track.id
                    && youtube_video_id(u).as_deref() != Some(track.id.as_str())
            })
            || track.is_available == Some(false)
            || matches!(
                track.availability.as_deref(),
                Some("private" | "premium_only" | "subscriber_only" | "needs_auth")
            )
            || matches!(track.title.as_str(), "[Deleted video]" | "[Private video]")
        {
            skipped += 1;
            continue;
        }
        if seen.insert(track.id.clone()) {
            result.push(track);
        }
    }
    (result, skipped)
}

#[derive(Default)]
pub struct Library {
    by_id: HashMap<String, String>,
    by_song: HashMap<(String, String), String>,
}
impl Library {
    pub fn add(&mut self, track: &Track) {
        if !audio_file_exists(&track.path) {
            return;
        }
        let stem = Path::new(&track.path)
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy();
        if let Some(pos) = stem.rfind(" [") {
            if let Some(id) = stem[pos + 2..].strip_suffix(']') {
                if valid_id(id) {
                    self.by_id
                        .entry(id.into())
                        .or_insert_with(|| track.path.clone());
                }
            }
        }
        for key in song_keys(track) {
            self.by_song
                .entry(key)
                .or_insert_with(|| track.path.clone());
        }
    }
    pub fn find(
        &self,
        track: &Track,
        downloaded: &HashMap<String, String>,
        preferred: Option<&Path>,
    ) -> Option<String> {
        let mut candidates: Vec<&String> = downloaded
            .get(&track.id)
            .into_iter()
            .chain(self.by_id.get(&track.id))
            .collect();
        candidates.extend(
            song_keys(track)
                .iter()
                .filter_map(|key| self.by_song.get(key)),
        );
        candidates.retain(|p| audio_file_exists(p));
        if let Some(dir) = preferred {
            if let Some(path) = candidates.iter().find(|p| {
                Path::new(p)
                    .parent()
                    .is_some_and(|parent| path_key(parent) == path_key(dir))
            }) {
                return Some((*path).to_string());
            }
        }
        candidates.first().map(|p| (*p).to_string())
    }
}
fn path_key(path: &Path) -> String {
    let path = fs::canonicalize(path)
        .unwrap_or_else(|_| path.to_path_buf())
        .to_string_lossy()
        .into_owned();
    if cfg!(windows) {
        path.to_lowercase()
    } else {
        path
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DownloadBudget {
    pub workers: usize,
    pub bytes_per_second: u64,
    pub gap: f64,
    pub limited: bool,
    pub label: &'static str,
}
pub fn budget_for(
    available: u64,
    memory_percent: f64,
    cpu_percent: f64,
    gaming: bool,
) -> DownloadBudget {
    let free = available as f64 / (1024u64.pow(3) as f64);
    let (workers, bytes_per_second, gap, limited, label) =
        if free < 1.0 || memory_percent >= 94.0 || cpu_percent >= 95.0 {
            (1, 64 * 1024, 15.0, true, "Very low resource use")
        } else if free < 2.0 || memory_percent >= 88.0 || cpu_percent >= 80.0 {
            (1, 256 * 1024, 8.0, true, "Low resource use")
        } else if gaming || free < 4.0 || cpu_percent >= 60.0 {
            (1, 512 * 1024, 4.0, true, "Low resource use")
        } else if free < 6.0 || cpu_percent >= 35.0 {
            (2, 2 * 1024 * 1024, 1.0, false, "Balanced downloads")
        } else {
            (3, 8 * 1024 * 1024, 0.5, false, "Idle downloads")
        };
    DownloadBudget {
        workers,
        bytes_per_second,
        gap,
        limited,
        label,
    }
}

/// Wire events retain all fields, including per-download progress and errors.
#[derive(Clone, Debug, Deserialize)]
pub struct ServiceEvent {
    pub event: String,
    #[serde(flatten)]
    pub data: serde_json::Map<String, Value>,
}
impl ServiceEvent {
    pub fn get(&self, name: &str) -> &Value {
        self.data.get(name).unwrap_or(&Value::Null)
    }
}

#[path = "youtube_native.rs"]
mod native_youtube;
pub use native_youtube::{YouTubeService, open_browser_profile_page};

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_video_urls_only() {
        assert_eq!(
            youtube_video_id("https://www.youtube.com/watch?v=abcdefghijk"),
            Some("abcdefghijk".into())
        );
        for url in [
            "https://evil.test/watch?v=abcdefghijk",
            "https://www.youtube.com/watch?v=abcdefghijk&v=abcdefghijk",
            "https://me@youtube.com/watch?v=abcdefghijk",
            "https://youtube.com:444/watch?v=abcdefghijk",
        ] {
            assert!(youtube_video_id(url).is_none());
        }
    }
    #[test]
    fn conservative_song_identity() {
        let a = Track {
            artist: "Test - Topic".into(),
            title: "Test - Song (Official Music Video) [Lyrics]".into(),
            ..Default::default()
        };
        let b = Track {
            artist: "Test".into(),
            title: "Song".into(),
            ..Default::default()
        };
        assert_eq!(song_keys(&a), song_keys(&b));
        let different = Track {
            artist: "Other".into(),
            title: "Song".into(),
            ..Default::default()
        };
        assert_ne!(song_keys(&a), song_keys(&different));
        assert!(song_keys(&Track {
            artist: "YouTube".into(),
            title: "Song".into(),
            ..Default::default()
        })
        .is_empty());
    }
    #[test]
    fn resource_pressure_wins_over_idle() {
        assert_eq!(
            budget_for(16 * 1024u64.pow(3), 20.0, 96.0, false).bytes_per_second,
            64 * 1024
        );
        assert_eq!(budget_for(16 * 1024u64.pow(3), 20.0, 5.0, true).workers, 1);
        assert_eq!(budget_for(16 * 1024u64.pow(3), 20.0, 5.0, false).workers, 3);
    }
    #[test]
    fn native_scan_reads_real_audio_and_skips_hidden_partial_files() {
        let base = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("state")
            .join("tmp")
            .join(format!("native-library-test-{}", std::process::id()));
        fs::create_dir_all(&base).unwrap();
        let mut wav = Vec::new();
        wav.extend(b"RIFF");
        wav.extend(16036u32.to_le_bytes());
        wav.extend(b"WAVEfmt ");
        wav.extend(16u32.to_le_bytes());
        wav.extend(1u16.to_le_bytes());
        wav.extend(1u16.to_le_bytes());
        wav.extend(8000u32.to_le_bytes());
        wav.extend(16000u32.to_le_bytes());
        wav.extend(2u16.to_le_bytes());
        wav.extend(16u16.to_le_bytes());
        wav.extend(b"data");
        wav.extend(16000u32.to_le_bytes());
        wav.resize(16044, 0);
        fs::write(base.join("Fixture.wav"), &wav).unwrap();
        fs::write(base.join(".hidden.wav"), &wav).unwrap();
        let playback_cache = base.join(".youtubeCached [abcdefghijk].wav");
        fs::write(&playback_cache, &wav).unwrap();
        assert!(!audio_file_exists(&playback_cache));
        let cached_track = track_from_path(&playback_cache)
            .expect("Hidden playback-cache files still need metadata");
        assert!((cached_track.duration.unwrap() - 1.0).abs() < 0.001);
        fs::write(base.join("partial.mp3"), []).unwrap();
        let directories = vec![
            base.to_string_lossy().into_owned(),
            base.to_string_lossy().into_owned(),
        ];
        let cache_path = base.join(".musicapp_meta.json");
        let songs = scan_library_cached(&directories, &cache_path);
        assert_eq!(songs.len(), 1);
        assert_eq!(songs[0].title, "Fixture");
        assert!((songs[0].duration.unwrap() - 1.0).abs() < 0.001);
        assert!(cache_path.is_file());
        assert_eq!(scan_library_cached(&directories, &cache_path).len(), 1);
        fs::remove_dir_all(base).unwrap();
    }
}
