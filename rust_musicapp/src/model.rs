use crate::services::Track;
use rand::{seq::SliceRandom, thread_rng};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    fs,
    path::Path,
};
pub fn load(path: &Path) -> Value {
    fs::read(path)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(json!({}))
}
pub fn save(path: &Path, value: &impl Serialize) -> Result<(), String> {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p).map_err(|e| e.to_string())?;
    }
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, serde_json::to_vec(value).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    // MoveFileEx replaces atomically on Windows; Rust rename supports replacing files.
    fs::rename(&tmp, path).map_err(|e| e.to_string())
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Binding {
    pub id: String,
    pub required: Vec<String>,
    #[serde(default = "yes")]
    pub alt_needed: bool,
    pub modifiable: bool,
}
fn yes() -> bool {
    true
}
/// A remembered visibility preference is applied separately from runtime readiness.
pub fn overlay_available(ready: bool, enabled: bool, loaded: bool, playing: bool, paused: bool, authenticating: bool) -> bool {
    ready && enabled && loaded && (playing || paused) && !authenticating
}
pub fn defaults() -> Vec<Binding> {
    [
        ("skip_previous", "alt", "-", true),
        ("skip_next", "alt", "=", true),
        ("volume_down", "alt", "[", true),
        ("volume_up", "alt", "]", true),
        ("pause_play", "alt", ";", true),
        ("repeat_toggle", "alt", "'", true),
        ("lyrics_toggle_visibility", "alt", "/", true),
        ("radio_enable_toggle", "alt", "a", true),
        ("radio_scan_station", "alt", "`", true),
        ("show_search", "alt", "\\", true),
        ("show_eq_menu", "right alt", "\\", true),
        ("youtube_sync", "right alt", "y", true),
        ("toggle_overlay", "alt", "shift", true),
        ("player_on_off", "right alt", "right shift", false),
        ("show_hints", "alt", "right alt", false),
        ("player_restart", "right alt", ".", false),
        ("kill_self", "right alt", "ctrl", false),
    ]
    .into_iter()
    .map(|(id, a, b, m)| Binding {
        id: id.into(),
        required: vec![a.into(), b.into()],
        alt_needed: true,
        modifiable: m,
    })
    .collect()
}
pub fn load_bindings(path: &Path) -> Vec<Binding> {
    let value = load(path);
    let mut list = defaults();
    for b in &mut list {
        if b.modifiable {
            if let Some(data) = value.get(&b.id) {
                if let Some(keys) = data["required"].as_array() {
                    let keys: Vec<_> = keys
                        .iter()
                        .filter_map(|v| v.as_str().map(|v| v.to_lowercase()))
                        .collect();
                    if !keys.is_empty() {
                        b.required = keys;
                    }
                }
                b.alt_needed = data["alt_needed"].as_bool().unwrap_or(true);
            }
        }
    }
    list
}
pub fn save_bindings(path: &Path, list: &[Binding]) -> Result<(), String> {
    let mut map = serde_json::Map::new();
    for b in list.iter().filter(|b| b.modifiable) {
        map.insert(
            b.id.clone(),
            json!({"required":b.required,"alt_needed":b.alt_needed}),
        );
    }
    save(path, &map)
}
pub fn chord(b: &Binding) -> HashSet<String> {
    b.required
        .iter()
        .filter(|k| **k != "alt" || b.alt_needed)
        .cloned()
        .collect()
}
pub fn triggered<'a>(bindings: &'a [Binding], pressed: &HashSet<String>) -> Option<&'a str> {
    let tracked: HashSet<_> = bindings
        .iter()
        .flat_map(|b| b.required.iter().cloned())
        .collect();
    bindings
        .iter()
        .find(|b| {
            let required = chord(b);
            !required.is_empty()
                && required.is_subset(pressed)
                && (b.id != "toggle_overlay"
                    || !pressed
                        .iter()
                        .any(|k| tracked.contains(k) && !required.contains(k)))
        })
        .map(|b| b.id.as_str())
}
pub fn remap(list: &mut [Binding], id: &str, key: &str) -> Result<(), String> {
    let pos = list
        .iter()
        .position(|b| b.id == id && b.modifiable)
        .ok_or("This shortcut is fixed")?;
    let side = if list[pos].required.iter().any(|s| s == "right alt")
        && !list[pos].required.iter().any(|s| s == "alt")
    {
        "right alt"
    } else {
        "alt"
    };
    let mut candidate = list[pos].clone();
    candidate.required = vec![side.into(), key.to_lowercase()];
    let keys = chord(&candidate);
    if let Some(other) = list.iter().find(|b| b.id != id && chord(b) == keys) {
        return Err(format!("Already used by {}", other.id));
    }
    list[pos] = candidate;
    Ok(())
}
pub struct Shuffler {
    upcoming: VecDeque<usize>,
    pub history: VecDeque<String>,
    artists: VecDeque<String>,
    pub replay: VecDeque<String>,
}
impl Shuffler {
    pub fn new() -> Self {
        Self {
            upcoming: VecDeque::new(),
            history: VecDeque::new(),
            artists: VecDeque::new(),
            replay: VecDeque::new(),
        }
    }
    pub fn refill(&mut self, n: usize) {
        let mut indexes: Vec<_> = (0..n).collect();
        indexes.shuffle(&mut thread_rng());
        self.upcoming = indexes.into();
    }
    pub fn choose(&mut self, library: &[Track]) -> Option<Track> {
        if let Some(path) = self.replay.pop_front() {
            if let Some(t) = library.iter().find(|t| t.path == path) {
                return Some(t.clone());
            }
        }
        if self.upcoming.is_empty() {
            self.refill(library.len());
        }
        for _ in 0..self.upcoming.len() {
            let idx = self.upcoming.pop_front()?;
            let Some(t) = library.get(idx) else { continue };
            let artist = t.artist.to_lowercase();
            if !self.history.contains(&t.path)
                && (artist.is_empty()
                    || artist == "unknown artist"
                    || !self.artists.contains(&artist))
            {
                if !artist.is_empty() && artist != "unknown artist" {
                    self.artists.push_back(artist);
                    while self.artists.len() > 2 {
                        self.artists.pop_front();
                    }
                }
                return Some(t.clone());
            }
            self.upcoming.push_back(idx);
        }
        let t = library.get(self.upcoming.pop_front()?)?.clone();
        self.artists.push_back(t.artist.to_lowercase());
        while self.artists.len() > 2 {
            self.artists.pop_front();
        }
        Some(t)
    }
    pub fn record(&mut self, t: &Track) {
        self.history.push_back(t.path.clone());
        while self.history.len() > 50 {
            self.history.pop_front();
        }
    }
}
pub fn clean_title(raw: &str) -> String {
    let mut core = raw.trim();
    let suffix = if core.ends_with("*=*") {
        core = &core[..core.len() - 3];
        " -[Paused]-"
    } else if core.ends_with("*+*") {
        core = &core[..core.len() - 3];
        " -[Repeat]-"
    } else {
        ""
    };
    let parts: Vec<_> = core
        .split(" - ")
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .collect();
    let (artist, title) = match parts.len() {
        0 => (String::new(), String::new()),
        1 => (String::new(), parts[0].to_string()),
        2 => (parts[0].into(), parts[1].into()),
        n if parts[0].to_lowercase() == parts[n - 1].to_lowercase() => {
            (parts[0].into(), parts[1..n - 1].join(" - "))
        }
        3 => (parts[1].into(), parts[2].into()),
        _ => (parts[0].into(), parts[1..].join(" - ")),
    };
    let end = [" - ", "(", "|", "["].iter().filter_map(|separator|title.find(separator)).min().unwrap_or(title.len());
    let first = title[..end].trim();
    let main = if first.is_empty() {
        title.as_str()
    } else {
        first
    };
    let text = [artist.trim(), main.trim()]
        .into_iter()
        .filter(|v| !v.is_empty())
        .collect::<Vec<_>>()
        .join(" - ");
    format!("{text}{suffix}")
        .replace("*=*", " -[Paused]-")
        .replace("*+*", " -[Repeat]-")
        .trim()
        .into()
}
#[derive(Serialize, Deserialize, Default)]
pub struct Recommendations {
    #[serde(default)]
    pub song_plays: HashMap<String, HashMap<String, u64>>,
    #[serde(default)]
    pub search_word_counts: HashMap<String, u64>,
}
impl Recommendations {
    pub fn random_suggestion(&self) -> String {
        use rand::Rng;
        let mut rng = thread_rng();
        match rng.gen_range(0..3) {
            0 => {
                let mut songs: Vec<_> = self
                    .song_plays
                    .iter()
                    .flat_map(|(artist, songs)| {
                        songs
                            .iter()
                            .map(move |(title, count)| (title, *count, artist))
                    })
                    .collect();
                songs.sort_by_key(|(_, n, _)| std::cmp::Reverse(*n));
                songs.truncate(5);
                songs
                    .choose(&mut rng)
                    .map(|(t, _, a)| format!("{} By {}", clean_title(t), a))
                    .unwrap_or_default()
            }
            1 => {
                let mut artists: Vec<_> = self
                    .song_plays
                    .iter()
                    .map(|(a, s)| (a, s.values().sum::<u64>()))
                    .collect();
                artists.sort_by_key(|(_, n)| std::cmp::Reverse(*n));
                artists.truncate(5);
                artists
                    .choose(&mut rng)
                    .map(|(a, _)| (*a).clone())
                    .unwrap_or_default()
            }
            _ => self.suggestion(),
        }
    }
    pub fn search(&mut self, query: &str) {
        if query.to_lowercase().contains("unknown") {
            return;
        }
        let stop = [
            "a", "an", "the", "and", "or", "but", "in", "on", "at", "for", "with", "about", "to",
            "from", "by", "of", "is", "it", "was", "were",
        ];
        for word in query
            .to_lowercase()
            .split(|c: char| !c.is_alphanumeric() && c != '_')
            .filter(|s| !s.is_empty() && !stop.contains(s))
        {
            *self.search_word_counts.entry(word.into()).or_default() += 1;
        }
    }
    pub fn played(&mut self, t: &Track) {
        if t.artist.is_empty() || t.title.is_empty() || t.artist.to_lowercase().contains("unknown")
        {
            return;
        }
        *self
            .song_plays
            .entry(t.artist.trim().to_lowercase())
            .or_default()
            .entry(t.title.trim().to_lowercase())
            .or_default() += 1;
    }
    pub fn suggestion(&self) -> String {
        let mut words: Vec<_> = self.search_word_counts.iter().collect();
        words.sort_by_key(|(_, v)| std::cmp::Reverse(**v));
        words
            .iter()
            .take(2)
            .map(|(w, _)| {
                let mut c = w.chars();
                c.next()
                    .map(|ch| ch.to_uppercase().collect::<String>() + c.as_str())
                    .unwrap_or_default()
            })
            .collect::<Vec<_>>()
            .join(" ")
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn overlay_waits_for_ready_track_and_keeps_paused_track() {
        assert!(!overlay_available(false, true, true, true, false, false));
        assert!(!overlay_available(true, true, false, true, false, false));
        assert!(!overlay_available(true, true, true, false, false, false));
        assert!(!overlay_available(true, true, true, true, false, true));
        assert!(!overlay_available(true, false, true, true, false, false));
        assert!(overlay_available(true, true, true, true, false, false));
        assert!(overlay_available(true, true, true, false, true, false));
    }
    #[test]
    fn chords_keep_sides_and_conflicts() {
        let mut b = defaults();
        assert!(remap(&mut b, "show_eq_menu", "y").is_err());
        remap(&mut b, "show_eq_menu", "k").unwrap();
        assert_eq!(
            b.iter().find(|b| b.id == "show_eq_menu").unwrap().required,
            vec!["right alt", "k"]
        );
    }
    #[test]
    fn overlay_forbids_other_tracked_keys() {
        let b = defaults();
        let p = ["alt", "shift", "right alt"]
            .map(str::to_string)
            .into_iter()
            .collect();
        assert_ne!(triggered(&b, &p), Some("toggle_overlay"));
    }
    #[test]
    fn titles_preserve_source_suffixes() {
        assert_eq!(
            clean_title("Context - Artist - Track (Official Video)*=*"),
            "Artist - Track -[Paused]-"
        );
        assert_eq!(
            clean_title("Artist - Song - Artist*+*"),
            "Artist - Song -[Repeat]-"
        );
    }
    #[test]
    fn shuffle_exhausts_small_library() {
        let l: Vec<_> = (0..3)
            .map(|i| Track {
                path: i.to_string(),
                artist: i.to_string(),
                ..Default::default()
            })
            .collect();
        let mut s = Shuffler::new();
        let mut seen = HashSet::new();
        for _ in 0..3 {
            let t = s.choose(&l).unwrap();
            seen.insert(t.path.clone());
            s.record(&t);
        }
        assert_eq!(seen.len(), 3);
    }
}
