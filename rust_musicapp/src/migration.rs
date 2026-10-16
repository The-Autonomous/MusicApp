//! Explicit, read-only import of the existing MusicApp profile into port state.
use crate::services::YouTubeService;
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

fn read_object(path: &Path) -> Value {
    fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}
fn sha256(bytes: &[u8]) -> Result<String, String> {
    #[link(name = "bcrypt")]
    extern "system" {
        fn BCryptOpenAlgorithmProvider(
            handle: *mut isize,
            algorithm: *const u16,
            implementation: *const u16,
            flags: u32,
        ) -> i32;
        fn BCryptHash(
            handle: isize,
            secret: *const u8,
            secret_len: u32,
            input: *const u8,
            input_len: u32,
            output: *mut u8,
            output_len: u32,
        ) -> i32;
        fn BCryptCloseAlgorithmProvider(handle: isize, flags: u32) -> i32;
    }
    let algorithm: Vec<u16> = "SHA256".encode_utf16().chain(Some(0)).collect();
    let mut handle = 0isize;
    unsafe {
        if BCryptOpenAlgorithmProvider(&mut handle, algorithm.as_ptr(), std::ptr::null(), 0) < 0 {
            return Err("Windows could not initialize profile hashing".into());
        }
        let mut digest = [0u8; 32];
        let status = BCryptHash(
            handle,
            std::ptr::null(),
            0,
            bytes.as_ptr(),
            bytes
                .len()
                .try_into()
                .map_err(|_| "Profile file too large")?,
            digest.as_mut_ptr(),
            32,
        );
        BCryptCloseAlgorithmProvider(handle, 0);
        if status < 0 {
            return Err("Windows could not hash a profile file".into());
        }
        Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
    }
}
fn changed_copy(
    path: &Path,
    bytes: &[u8],
    backup: &Path,
    backup_name: &str,
) -> Result<bool, String> {
    if fs::read(path).ok().as_deref() == Some(bytes) {
        return Ok(false);
    }
    if path.is_file() {
        let previous = backup.join(backup_name);
        if let Some(parent) = previous.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        fs::copy(path, previous).map_err(|e| e.to_string())?;
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let temporary = path.with_extension("import.tmp");
    fs::write(&temporary, bytes).map_err(|e| e.to_string())?;
    fs::rename(temporary, path).map_err(|e| e.to_string())?;
    Ok(true)
}
fn preferences(state: &Value, overlay: &Value, sync: &Value) -> Value {
    let mut result = json!({"path":null,"elapsed":0.0,"paused":false,"repeat":false,"volume":0.5,"gaming_mode":true,"accept_radio_eq":false,"current_radio_ip":"0.0.0.0","youtube_download_permanently":false,"do_youtube_search":true,"visible":true,"player_enabled":true,"position":null,"source":"YouTube Music liked songs","host_radio":false,"auto_gaming":true,"lyrics_visible":true});
    for name in [
        "path",
        "elapsed",
        "paused",
        "repeat",
        "volume",
        "gaming_mode",
        "accept_radio_eq",
        "current_radio_ip",
        "youtube_download_permanently",
        "do_youtube_search",
    ] {
        if let Some(value) = state.get(name) {
            result[name] = value.clone();
        }
    }
    for name in ["visible", "player_enabled", "position"] {
        if let Some(value) = overlay.get(name) {
            result[name] = value.clone();
        }
    }
    for name in ["source", "host_radio", "auto_gaming"] {
        if let Some(value) = sync.get(name) {
            result[name] = value.clone();
        }
    }
    result
}
fn metadata(mut data: Value) -> Value {
    if let Some(entries) = data.as_object_mut() {
        for (path, record) in entries {
            if let Some(record) = record.as_object_mut() {
                record
                    .entry("path")
                    .or_insert_with(|| Value::String(path.clone()));
                if let Some(value) = record.get("mtime").and_then(Value::as_f64) {
                    if value.is_finite()
                        && value >= 0.0
                        && record.get("mtime").and_then(Value::as_u64).is_none()
                    {
                        let ns = if value < 100_000_000_000_000.0 {
                            // Multiplying the full epoch float would discard
                            // nanoseconds even for exactly representable .25s.
                            (value.trunc() as u64)
                                .checked_mul(1_000_000_000)
                                .and_then(|whole| {
                                    whole.checked_add(
                                        (value.fract() * 1_000_000_000.0).round() as u64
                                    )
                                })
                        } else {
                            (value <= u64::MAX as f64).then_some(value as u64)
                        };
                        if let Some(ns) = ns {
                            record.insert("mtime".into(), Value::from(ns));
                        }
                    }
                }
            }
        }
    }
    data
}

/// Import is explicit and idempotent. Originals are read only, credentials are
/// never emitted, and replaced port files have their own recoverable backup.
pub fn import_profile(source: &Path, port_root: &Path, state: &Path) -> Result<Value, String> {
    let source =
        fs::canonicalize(source).map_err(|e| format!("Cannot locate the original profile: {e}"))?;
    let port_root = std::path::absolute(port_root).map_err(|e| e.to_string())?;
    let state = std::path::absolute(state).map_err(|e| e.to_string())?;
    if fs::canonicalize(&port_root).ok().as_deref() == Some(source.as_path())
        || fs::canonicalize(&state).ok().as_deref() == Some(source.as_path())
    {
        return Err(
            "The import destination must be separate from the original MusicApp profile".into(),
        );
    }
    fs::create_dir_all(&state).map_err(|e| e.to_string())?;
    let id = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let backup = state.join("import-backup").join(id.to_string());
    let playback = read_object(&source.join(".musicapp_state.json"));
    let overlay = read_object(&source.join(".musicapp_overlay.json"));
    let sync = read_object(&source.join(".youtube_sync.json"));
    let expected = preferences(&playback, &overlay, &sync);
    let prefbytes = serde_json::to_vec_pretty(&expected).map_err(|e| e.to_string())?;
    let mut changed = usize::from(changed_copy(
        &state.join("preferences.json"),
        &prefbytes,
        &backup,
        "state/preferences.json",
    )?);
    let mut records = Vec::new();
    for name in [
        ".musicapp_state.json",
        ".musicapp_overlay.json",
        ".youtube_sync.json",
    ] {
        if let Ok(bytes) = fs::read(source.join(name)) {
            records.push(json!({"name":name,"source_sha256":sha256(&bytes)?,"source_bytes":bytes.len(),"merged":true}));
        }
    }
    let files = [
        "musicapp_eq.json",
        ".keyBindings.json",
        ".player_recommend_data.json",
        ".lyricCache.json",
        ".youtube_likes.json",
        ".youtube_downloads.json",
        ".youtube_deferred_downloads.json",
        ".youtube_sync.json",
        ".musicapp_meta.json",
    ];
    let mut metadata_entries = 0;
    for name in files {
        let Ok(bytes) = fs::read(source.join(name)) else {
            continue;
        };
        let target = state.join(name);
        let mut transformed = false;
        let payload = if name == ".musicapp_meta.json" {
            let value: Value = serde_json::from_slice(&bytes)
                .map_err(|_| "The original metadata cache is not valid JSON")?;
            let value = metadata(value);
            metadata_entries = value.as_object().map(|v| v.len()).unwrap_or(0);
            transformed = true;
            serde_json::to_vec(&value).map_err(|e| e.to_string())?
        } else {
            bytes.clone()
        };
        let did_change = changed_copy(&target, &payload, &backup, &format!("state/{name}"))?;
        changed += usize::from(did_change);
        records.push(json!({"name":name,"source_sha256":sha256(&bytes)?,"source_bytes":bytes.len(),"target_sha256":sha256(&payload)?,"changed":did_change,"schema_normalized":transformed}));
    }
    if let Ok(bytes) = fs::read(source.join("Playlists.txt")) {
        let did_change = changed_copy(
            &port_root.join("Playlists.txt"),
            &bytes,
            &backup,
            "root/Playlists.txt",
        )?;
        changed += usize::from(did_change);
        records.push(json!({"name":"Playlists.txt","source_sha256":sha256(&bytes)?,"source_bytes":bytes.len(),"changed":did_change}));
    }
    let mut session = json!({"source_exists":false,"session_origin":null,"encrypted_copy_saved":false,"imported":false,"verified":false});
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        let original = PathBuf::from(local).join("MusicApp/youtube_session.bin");
        if let Ok(bytes) = fs::read(original) {
            session["source_exists"] = true.into();
            session["session_origin"] = "python_saved_session".into();
            let encrypted_copy = state.join("imported_python_youtube_session.bin");
            let did_change = changed_copy(
                &encrypted_copy,
                &bytes,
                &backup,
                "state/imported_python_youtube_session.bin",
            )?;
            changed += usize::from(did_change);
            session["encrypted_copy_saved"] = true.into();
            // Back up an existing native session before RustyPipe updates its own
            // separately DPAPI-protected cache. Never hash or print credentials.
            let native = state.join("youtube_session.bin");
            if native.is_file() {
                let previous = backup.join("state/youtube_session.bin");
                if let Some(parent) = previous.parent() {
                    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
                }
                fs::copy(&native, previous).map_err(|e| e.to_string())?;
            }
            match YouTubeService::spawn_with_state(&port_root, &state)
                .and_then(|mut service| service.import_cookie_session(&bytes))
            {
                Ok(details) => {
                    session["imported"] = true.into();
                    session["verified"] = details["verified"].clone();
                    session["cookie_count"] = details["cookie_count"].clone();
                    session["cookie_values_match"] = details["cookie_values_match"].clone();
                    session["remembered"] = details["remembered"].clone();
                }
                Err(_) => {
                    session["error"]="The encrypted source session was preserved, but YouTube could not verify it. No browser sign-in was started.".into();
                }
            }
        }
    }
    if session["source_exists"] == false {
        session["session_origin"] = "none".into();
        session["error"] = "No saved MusicApp session exists. Use Chrome/Edge sign-in, or connect-browser.bat to connect your existing browser profile.".into();
    }
    let semantic_match = read_object(&state.join("preferences.json")) == expected;
    let report = json!({"imported":true,"settings_match":semantic_match,"changed_files":changed,"files":records,"metadata_entries":metadata_entries,"youtube_session":session,"backup":if backup.exists(){Some(backup.to_string_lossy().into_owned())}else{None}});
    let bytes = serde_json::to_vec_pretty(&report).map_err(|e| e.to_string())?;
    let _ = changed_copy(
        &state.join("profile_import_report.json"),
        &bytes,
        &backup,
        "state/profile_import_report.json",
    )?;
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn settings_merge_keeps_personal_values_and_factory_defaults() {
        let merged = preferences(
            &json!({"volume":0.23,"paused":true,"gaming_mode":false}),
            &json!({"visible":false,"position":[-600,40]}),
            &json!({"source":"YouTube liked videos","host_radio":true}),
        );
        assert_eq!(merged["volume"], 0.23);
        assert_eq!(merged["paused"], true);
        assert_eq!(merged["visible"], false);
        assert_eq!(merged["position"], json!([-600, 40]));
        assert_eq!(merged["source"], "YouTube liked videos");
        assert_eq!(merged["auto_gaming"], true);
    }
    #[test]
    fn native_sha256_known_vector() {
        assert_eq!(
            sha256(b"abc").unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
    #[test]
    fn legacy_metadata_seconds_become_native_nanoseconds() {
        let value = metadata(
            json!({"song.wav":{"mtime":1700000000.25,"size":100,"duration":1.0,"title":"Song"}}),
        );
        assert_eq!(value["song.wav"]["mtime"], 1_700_000_000_250_000_000u64);
        assert_eq!(value["song.wav"]["path"], "song.wav");
        let current =
            metadata(json!({"current.wav":{"mtime":1_700_000_000_250_000_123u64,"size":100}}));
        assert_eq!(
            current["current.wav"]["mtime"],
            1_700_000_000_250_000_123u64
        );
    }
}
