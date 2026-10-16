//! One native lyric worker with source-compatible cache and latest-only queue.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::HashMap,
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::{mpsc::Sender, Arc, Condvar, Mutex},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const POSITIVE_TTL: f64 = 168.0 * 3600.0;
const NEGATIVE_TTL: f64 = 6.0 * 3600.0;
const MAX_ENTRIES: usize = 1000;
const MAX_RESPONSE_BYTES: u64 = 256 * 1024;
type Lines = Vec<(f64, String)>;
fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn cache_key(artist: &str, title: &str) -> String {
    format!(
        "{}|{}",
        artist.trim().to_lowercase(),
        title.trim().to_lowercase()
    )
}

#[derive(Clone, Serialize, Deserialize)]
struct CachedLine {
    timestamp: f64,
    text: String,
}
#[derive(Clone, Serialize, Deserialize)]
struct CacheEntry {
    timestamp: f64,
    lyrics: Vec<CachedLine>,
}
impl CacheEntry {
    fn fresh(&self, current: f64) -> bool {
        let ttl = if self.lyrics.is_empty() {
            NEGATIVE_TTL
        } else {
            POSITIVE_TTL
        };
        self.timestamp.is_finite() && current - self.timestamp < ttl
    }
    fn lines(&self) -> Lines {
        self.lyrics
            .iter()
            .map(|line| (line.timestamp, line.text.clone()))
            .collect()
    }
}
#[derive(Default)]
struct Cache {
    entries: HashMap<String, CacheEntry>,
}
impl Cache {
    fn load(path: &Path, current: f64) -> Self {
        let mut entries: HashMap<String, CacheEntry> = fs::read(path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .and_then(|value| value.as_object().cloned())
            .map(|object| {
                object
                    .into_iter()
                    .filter_map(|(key, value)| {
                        let entry = serde_json::from_value::<CacheEntry>(value).ok()?;
                        (entry.fresh(current)
                            && entry.lyrics.iter().all(|l| l.timestamp.is_finite()))
                        .then_some((key, entry))
                    })
                    .collect()
            })
            .unwrap_or_default();
        Self::bound(&mut entries);
        Self { entries }
    }
    fn bound(entries: &mut HashMap<String, CacheEntry>) {
        while entries.len() > MAX_ENTRIES {
            let oldest = entries
                .iter()
                .min_by(|(_, a), (_, b)| a.timestamp.total_cmp(&b.timestamp))
                .map(|(key, _)| key.clone());
            if let Some(key) = oldest {
                entries.remove(&key);
            } else {
                break;
            }
        }
    }
    fn get(&self, artist: &str, title: &str, current: f64) -> Option<Lines> {
        self.entries
            .get(&cache_key(artist, title))
            .filter(|e| e.fresh(current))
            .map(CacheEntry::lines)
    }
    fn add(&mut self, artist: &str, title: &str, lyrics: &Lines, current: f64) {
        self.entries.insert(
            cache_key(artist, title),
            CacheEntry {
                timestamp: current,
                lyrics: lyrics
                    .iter()
                    .map(|(timestamp, text)| CachedLine {
                        timestamp: *timestamp,
                        text: text.clone(),
                    })
                    .collect(),
            },
        );
        Self::bound(&mut self.entries);
    }
    fn save(&self, path: &Path) {
        let _ = crate::model::save(path, &self.entries);
    }
}
struct Request {
    id: u64,
    artist: String,
    title: String,
}
#[derive(Default)]
struct QueueState {
    pending: Option<Request>,
    shutdown: bool,
}
#[derive(Default)]
struct LatestQueue {
    state: Mutex<QueueState>,
    wake: Condvar,
}
impl LatestQueue {
    fn push(&self, request: Request) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.shutdown {
            return;
        }
        state.pending = Some(request);
        self.wake.notify_one();
    }
    fn next(&self) -> Option<Request> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            if state.shutdown {
                return None;
            }
            if let Some(request) = state.pending.take() {
                return Some(request);
            }
            state = self.wake.wait(state).unwrap_or_else(|e| e.into_inner());
        }
    }
    fn stopped(&self) -> bool {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .shutdown
    }
    fn wait_cancelled(&self, duration: Duration) -> bool {
        let until = Instant::now() + duration;
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        while !state.shutdown {
            let remaining = until.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return false;
            }
            let (next, _) = self
                .wake
                .wait_timeout(state, remaining)
                .unwrap_or_else(|e| e.into_inner());
            state = next;
        }
        true
    }
    fn close(&self) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.shutdown = true;
        state.pending = None;
        self.wake.notify_all();
    }
}
fn parse_lyrics(raw: &str) -> Lines {
    static TIMESTAMP: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$").unwrap());
    let mut lines: Lines = raw
        .lines()
        .filter_map(|line| {
            let captures = TIMESTAMP.captures(line)?;
            let stamp =
                captures[1].parse::<f64>().ok()? * 60.0 + captures[2].parse::<f64>().ok()?;
            if !stamp.is_finite() {
                return None;
            }
            let text = captures[3].trim();
            Some((
                stamp,
                if text.is_empty() {
                    "🎵".into()
                } else {
                    text.into()
                },
            ))
        })
        .collect();
    lines.sort_by(|(a, text_a), (b, text_b)| a.total_cmp(b).then_with(|| text_a.cmp(text_b)));
    lines
}
fn clean_request(artist: &str, title: &str) -> (String, String) {
    let cleaned = crate::model::clean_title(&format!("{artist} - {title}"));
    if let Some((artist, title)) = cleaned.split_once(" - ") {
        (artist.into(), title.into())
    } else {
        (artist.into(), cleaned)
    }
}
fn fetch_lyrics(
    client: &reqwest::blocking::Client,
    endpoint: &str,
    artist: &str,
    title: &str,
    queue: &LatestQueue,
    last_request: &mut Instant,
) -> Lines {
    if queue.wait_cancelled(Duration::from_secs(1).saturating_sub(last_request.elapsed())) {
        return Vec::new();
    }
    let (artist, title) = clean_request(artist, title);
    for attempt in 0..=1 {
        if queue.stopped() {
            return Vec::new();
        }
        *last_request = Instant::now();
        match client
            .get(endpoint)
            .query(&[("artist_name", &artist), ("track_name", &title)])
            .send()
        {
            Ok(response) if response.status().as_u16() == 404 => return Vec::new(),
            Ok(response) if response.status().as_u16() == 429 => {
                drop(response);
                if queue.wait_cancelled(Duration::from_secs(2)) {
                    return Vec::new();
                }
                continue;
            }
            Ok(response) => {
                if let Ok(response) = response.error_for_status() {
                    let mut bytes = Vec::new();
                    let read = response
                        .take(MAX_RESPONSE_BYTES + 1)
                        .read_to_end(&mut bytes);
                    if queue.stopped() {
                        return Vec::new();
                    }
                    if read.is_ok() && bytes.len() as u64 <= MAX_RESPONSE_BYTES {
                        if let Ok(data) = serde_json::from_slice::<Value>(&bytes) {
                            return parse_lyrics(data["syncedLyrics"].as_str().unwrap_or(""));
                        }
                    }
                }
            }
            Err(_) => (),
        }
        if attempt < 1 && queue.wait_cancelled(Duration::from_secs(1)) {
            return Vec::new();
        }
    }
    Vec::new()
}

pub struct Lyrics {
    queue: Arc<LatestQueue>,
    join: Option<thread::JoinHandle<()>>,
}
impl Lyrics {
    /// `directory` is the actual state directory, including --state-dir overrides.
    pub fn new(directory: PathBuf, events: Sender<Vec<String>>) -> Self {
        let queue = Arc::new(LatestQueue::default());
        let worker_queue = queue.clone();
        let join = thread::spawn(move || {
            let cache_path = directory.join(".lyricCache.json");
            let mut cache = Cache::load(&cache_path, now());
            let client = reqwest::blocking::Client::builder()
                .connect_timeout(Duration::from_secs(3))
                .timeout(Duration::from_secs(9))
                .user_agent("MusicApp/lyrics")
                .build()
                .ok();
            let mut last_request = Instant::now() - Duration::from_secs(2);
            while let Some(request) = worker_queue.next() {
                let lines = if let Some(lines) = cache.get(&request.artist, &request.title, now()) {
                    lines
                } else {
                    let lines = client
                        .as_ref()
                        .map(|client| {
                            fetch_lyrics(
                                client,
                                "https://lrclib.net/api/get",
                                &request.artist,
                                &request.title,
                                &worker_queue,
                                &mut last_request,
                            )
                        })
                        .unwrap_or_default();
                    if worker_queue.stopped() {
                        break;
                    }
                    cache.add(&request.artist, &request.title, &lines, now());
                    cache.save(&cache_path);
                    lines
                };
                if worker_queue.stopped() {
                    break;
                }
                let _ = events.send(vec![
                    "lyrics_result".into(),
                    request.id.to_string(),
                    serde_json::to_string(&lines).unwrap_or_else(|_| "[]".into()),
                ]);
            }
        });
        Self {
            queue,
            join: Some(join),
        }
    }
    pub fn request(&self, id: u64, artist: &str, title: &str) {
        self.queue.push(Request {
            id,
            artist: artist.into(),
            title: title.into(),
        });
    }
}
impl Drop for Lyrics {
    fn drop(&mut self) {
        self.queue.close();
        if let Some(join) = self.join.take() {
            let deadline = Instant::now() + Duration::from_millis(750);
            while !join.is_finished() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
            }
            // An in-flight blocking HTTP call has a fixed deadline; detach it
            // on close rather than drain old tracks or delay application exit.
            if join.is_finished() {
                let _ = join.join();
            }
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cache_preserves_schema_and_distinct_positive_negative_expiry() {
        let mut cache = Cache::default();
        let at = 2_000_000.0;
        cache.add(" Artist ", " Song ", &vec![(1.5, "line".into())], at);
        cache.add("Artist", "Missing", &vec![], at);
        assert!(cache
            .get("artist", "song", at + POSITIVE_TTL - 1.0)
            .is_some());
        assert!(cache.get("artist", "song", at + POSITIVE_TTL).is_none());
        assert_eq!(
            cache.get("artist", "missing", at + NEGATIVE_TTL - 1.0),
            Some(vec![])
        );
        assert!(cache.get("artist", "missing", at + NEGATIVE_TTL).is_none());
        let value = serde_json::to_value(&cache.entries).unwrap();
        assert_eq!(value["artist|song"]["lyrics"][0]["timestamp"], 1.5);
        assert_eq!(value["artist|song"]["lyrics"][0]["text"], "line");
    }
    #[test]
    fn bounded_cache_evicts_oldest() {
        let mut cache = Cache::default();
        for i in 0..=MAX_ENTRIES {
            cache.add("A", &i.to_string(), &vec![], i as f64);
        }
        assert_eq!(cache.entries.len(), MAX_ENTRIES);
        assert!(!cache.entries.contains_key("a|0"));
        assert!(cache.entries.contains_key(&format!("a|{MAX_ENTRIES}")));
    }
    #[test]
    fn parser_sorts_and_retains_instrumental_lines() {
        assert_eq!(
            parse_lyrics("[00:03.5] Later\n[ar:Artist]\n[00:01] \n[00:03.5] Before"),
            vec![
                (1.0, "🎵".into()),
                (3.5, "Before".into()),
                (3.5, "Later".into())
            ]
        );
        assert_eq!(
            clean_request("Artist", "Track (Official Video)"),
            ("Artist".into(), "Track".into())
        );
    }
    #[test]
    fn queue_keeps_latest_request_and_cancels_waits() {
        let queue = LatestQueue::default();
        for id in 1..=100 {
            queue.push(Request {
                id,
                artist: "a".into(),
                title: "t".into(),
            });
        }
        assert_eq!(queue.next().unwrap().id, 100);
        queue.close();
        assert!(queue.next().is_none());
        assert!(queue.wait_cancelled(Duration::from_secs(100)));
    }
    #[test]
    fn request_retries_once_without_external_network() {
        use std::io::Write;
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = format!("http://{}/lyrics", listener.local_addr().unwrap());
        let server = thread::spawn(move || {
            for response in ["HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_string(), {
                let body = r#"{"syncedLyrics":"[00:01] Recovered"}"#; format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()) }] {
                let (mut socket, _) = listener.accept().unwrap(); socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
                let mut bytes = [0; 8192]; let _ = socket.read(&mut bytes).unwrap(); socket.write_all(response.as_bytes()).unwrap();
            }
        });
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(3))
            .build()
            .unwrap();
        let queue = LatestQueue::default();
        let mut previous = Instant::now() - Duration::from_secs(2);
        assert_eq!(
            fetch_lyrics(&client, &endpoint, "Artist", "Track", &queue, &mut previous),
            vec![(1.0, "Recovered".into())]
        );
        server.join().unwrap();
    }
}
