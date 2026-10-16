//! Bounded playback: decoding and DSP never execute on the device callback.
//! The original Python player remains independent of every file written here.
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use rustfft::{num_complex::Complex32, Fft, FftPlanner};
use serde::{Deserialize, Serialize};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub const SAMPLE_RATE: u32 = 44_100;
pub const CHUNK: usize = 1024;
pub const ISO_BANDS: [u32; 10] = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
const CAPACITY: usize = SAMPLE_RATE as usize / 2;

/// SPSC frame ring. Atomic packed stereo frames avoid unsafe memory and locks.
/// Producer publishes its write cursor only after publishing the sample.
struct FrameRing {
    data: Box<[AtomicU64]>,
    read: AtomicUsize,
    write: AtomicUsize,
}
impl FrameRing {
    fn new(capacity: usize) -> Self {
        Self { data: (0..capacity + 1).map(|_| AtomicU64::new(0)).collect(), read: AtomicUsize::new(0), write: AtomicUsize::new(0) }
    }
    fn push(&self, frame: [f32; 2]) -> bool {
        let write = self.write.load(Ordering::Relaxed);
        let next = (write + 1) % self.data.len();
        if next == self.read.load(Ordering::Acquire) { return false; }
        let bits = frame[0].to_bits() as u64 | ((frame[1].to_bits() as u64) << 32);
        self.data[write].store(bits, Ordering::Relaxed);
        self.write.store(next, Ordering::Release);
        true
    }
    fn pop(&self) -> Option<[f32; 2]> {
        let read = self.read.load(Ordering::Relaxed);
        if read == self.write.load(Ordering::Acquire) { return None; }
        let bits = self.data[read].load(Ordering::Relaxed);
        self.read.store((read + 1) % self.data.len(), Ordering::Release);
        Some([f32::from_bits(bits as u32), f32::from_bits((bits >> 32) as u32)])
    }
    fn len(&self) -> usize {
        let read = self.read.load(Ordering::Acquire);
        let write = self.write.load(Ordering::Acquire);
        (write + self.data.len() - read) % self.data.len()
    }
    // Called only after cancellation and pausing the output device.
    fn clear(&self) { self.read.store(self.write.load(Ordering::Acquire), Ordering::Release); }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
pub struct EchoParams { pub delay_ms: f32, pub feedback: f32, pub wet: f32 }
impl Default for EchoParams {
    fn default() -> Self { Self { delay_ms: 350., feedback: 0.35, wet: 0.5 } }
}
impl EchoParams {
    fn clamped(self) -> Self { Self { delay_ms: self.delay_ms.clamp(10., 5000.), feedback: self.feedback.clamp(0., 0.95), wet: self.wet.clamp(0., 1.) } }
}
#[derive(Clone, Copy, Default)]
struct Effects { gains: [f32; 10], echo: Option<EchoParams>, echo_revision: u64 }
struct Shared {
    ring: FrameRing,
    active: AtomicBool,
    paused: AtomicBool,
    decoded: AtomicBool,
    ended: AtomicBool,
    volume: AtomicU32,
    gaming: AtomicBool,
    frames: AtomicU64,
    underflows: AtomicU64,
    generation: AtomicU64,
    callbacks: AtomicUsize,
    effects: Mutex<Effects>,
    error: Mutex<Option<String>>,
}
impl Shared {
    fn new() -> Self {
        Self { ring: FrameRing::new(CAPACITY), active: AtomicBool::new(false), paused: AtomicBool::new(false), decoded: AtomicBool::new(false), ended: AtomicBool::new(false), volume: AtomicU32::new(0.1f32.to_bits()), gaming: AtomicBool::new(true), frames: AtomicU64::new(0), underflows: AtomicU64::new(0), generation: AtomicU64::new(0), callbacks: AtomicUsize::new(0), effects: Mutex::new(Effects::default()), error: Mutex::new(None) }
    }
}

/// Creates no device or decoder until the first track is played.
pub struct AudioEngine {
    base: PathBuf,
    shared: Arc<Shared>,
    stream: Option<cpal::Stream>,
    cancel: Arc<AtomicBool>,
    child: Arc<Mutex<Option<Child>>>,
    reader: Option<JoinHandle<()>>,
    path: Option<String>,
    duration: f64,
    eq_dirty: bool,
}
impl AudioEngine {
    pub fn new(base: PathBuf) -> Self {
        let shared = Arc::new(Shared::new());
        // Only the Rust application's own settings are read. Factory default is flat.
        if let Ok(data) = std::fs::read(base.join("musicapp_eq.json")) {
            if let Ok(gains) = serde_json::from_slice::<std::collections::HashMap<String, f32>>(&data) {
                let mut effects = shared.effects.lock().unwrap_or_else(|e| e.into_inner());
                for (i, band) in ISO_BANDS.iter().enumerate() {
                    if let Some(gain) = gains.get(&band.to_string()).filter(|v| v.is_finite()) { effects.gains[i] = gain.clamp(-12., 12.); }
                }
            }
        }
        Self { base, shared, stream: None, cancel: Arc::new(AtomicBool::new(false)), child: Arc::new(Mutex::new(None)), reader: None, path: None, duration: 0., eq_dirty: false }
    }

    pub fn play(&mut self, path: &str, start_pos: f64) -> Result<(), String> {
        self.play_with_duration(path, start_pos, None)
    }
    /// A finite, positive duration already read by the library avoids reopening
    /// metadata or starting a probe process. Playback/decoding stays identical.
    pub fn play_with_duration(&mut self, path: &str, start_pos: f64, duration: Option<f64>) -> Result<(), String> {
        self.start(path, start_pos, false, duration)
    }
    fn start(&mut self, path: &str, start_pos: f64, paused: bool, duration_hint: Option<f64>) -> Result<(), String> {
        self.stop();
        if !path.starts_with("http://") && !path.starts_with("https://") && !Path::new(path).is_file() {
            return Err(format!("Audio file does not exist: {path}"));
        }
        self.ensure_device()?;
        let ffmpeg = find_tool(&self.base, "ffmpeg");
        let cached_duration = (self.path.as_deref() == Some(path)).then_some(self.duration);
        self.duration = resolve_duration(duration_hint, cached_duration,
            || local_duration(path),
            || probe_duration(&find_tool(&self.base, "ffprobe"), path)
                .or_else(|| probe_ffmpeg_duration(&ffmpeg, path)));
        let mut start = if start_pos.is_finite() { start_pos.max(0.) } else { 0. };
        if self.duration > 0. { start = start.min(self.duration); }
        let mut command = hidden_command(&ffmpeg);
        command.args(["-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", "-filter_threads", "1"]);
        if path.starts_with("http://") || path.starts_with("https://") { command.args(["-rw_timeout", "3000000"]); }
        if start > 0. { command.args(["-ss", &format!("{start:.9}")]); }
        command.args(["-i", path, "-vn", "-sn", "-dn", "-map", "0:a:0", "-threads", "1", "-ac", "2", "-ar", "44100", "-f", "f32le", "pipe:1"]);
        let mut child = command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().map_err(|e| format!("Cannot start FFmpeg ({}): {e}. Run setup_tools.ps1 first.", ffmpeg.display()))?;
        let stdout = child.stdout.take().ok_or("FFmpeg audio pipe was unavailable")?;
        *self.child.lock().unwrap_or_else(|e| e.into_inner()) = Some(child);
        self.path = Some(path.to_owned());
        self.cancel = Arc::new(AtomicBool::new(false));
        self.shared.frames.store((start * SAMPLE_RATE as f64) as u64, Ordering::Release);
        self.shared.decoded.store(false, Ordering::Release);
        self.shared.ended.store(false, Ordering::Release);
        self.shared.paused.store(paused, Ordering::Release);
        self.shared.underflows.store(0, Ordering::Relaxed);
        *self.shared.error.lock().unwrap_or_else(|e| e.into_inner()) = None;
        let shared = self.shared.clone();
        let cancel = self.cancel.clone();
        let process = self.child.clone();
        match thread::Builder::new().name("audio-decode".into()).spawn(move || decode(stdout, shared, cancel, process)) {
            Ok(reader) => self.reader = Some(reader),
            Err(error) => { self.stop(); return Err(format!("Cannot start audio decoder thread: {error}")); }
        }
        let deadline = Instant::now() + Duration::from_secs(3);
        while self.shared.ring.len() < CHUNK * 2 && !self.shared.decoded.load(Ordering::Acquire) && Instant::now() < deadline { thread::sleep(Duration::from_millis(2)); }
        if self.shared.ring.len() == 0 {
            let message = self.last_error().unwrap_or_else(|| "No audio samples could be decoded".into());
            self.stop();
            return Err(message);
        }
        self.shared.active.store(true, Ordering::Release);
        if !paused {
            if let Err(error) = self.stream.as_ref().unwrap().play() {
                self.stop(); self.stream.take();
                return Err(format!("Cannot start audio device: {error}"));
            }
        }
        Ok(())
    }
    fn ensure_device(&mut self) -> Result<(), String> {
        if self.stream.is_some() { return Ok(()); }
        let host = cpal::default_host();
        let device = host.default_output_device().ok_or("No default audio output device")?;
        let preferred = device.supported_output_configs().map_err(|e| e.to_string())?.find(|r| r.channels() == 2 && r.sample_format() == cpal::SampleFormat::F32 && r.min_sample_rate().0 <= SAMPLE_RATE && r.max_sample_rate().0 >= SAMPLE_RATE).map(|r| r.with_sample_rate(cpal::SampleRate(SAMPLE_RATE)));
        let supported = match preferred { Some(config) => config, None => device.default_output_config().map_err(|e| e.to_string())? };
        let format = supported.sample_format();
        let mut config: cpal::StreamConfig = supported.into();
        // Default buffer size avoids rejecting devices whose period is fixed by Windows.
        config.buffer_size = cpal::BufferSize::Default;
        self.stream = Some(match format {
            cpal::SampleFormat::F32 => build_stream::<f32>(&device, &config, self.shared.clone()),
            cpal::SampleFormat::I16 => build_stream::<i16>(&device, &config, self.shared.clone()),
            cpal::SampleFormat::U16 => build_stream::<u16>(&device, &config, self.shared.clone()),
            cpal::SampleFormat::I32 => build_stream::<i32>(&device, &config, self.shared.clone()),
            cpal::SampleFormat::F64 => build_stream::<f64>(&device, &config, self.shared.clone()),
            _ => return Err(format!("Unsupported output sample format: {format:?}")),
        }?);
        Ok(())
    }
    pub fn stop(&mut self) {
        self.shared.active.store(false, Ordering::Release);
        self.cancel.store(true, Ordering::Release);
        if let Some(reader) = &self.reader { reader.thread().unpark(); }
        if let Some(stream) = &self.stream { let _ = stream.pause(); }
        if let Some(child) = self.child.lock().unwrap_or_else(|e| e.into_inner()).as_mut() { let _ = child.kill(); }
        if let Some(reader) = self.reader.take() { let _ = reader.join(); }
        if let Some(mut child) = self.child.lock().unwrap_or_else(|e| e.into_inner()).take() { let _ = child.wait(); }
        // CPAL pause requests can be asynchronous. Finish an already-running
        // callback before resetting the ring/clock for a replacement session.
        while self.shared.callbacks.load(Ordering::Acquire) != 0 { thread::yield_now(); }
        self.shared.ring.clear();
        self.shared.generation.fetch_add(1, Ordering::Release);
        self.shared.paused.store(false, Ordering::Release);
        self.shared.frames.store(0, Ordering::Release);
        self.shared.ended.store(false, Ordering::Release);
    }
    pub fn pause(&self) {
        self.shared.paused.store(true, Ordering::Release);
        if let Some(stream) = &self.stream { let _ = stream.pause(); }
        while self.shared.callbacks.load(Ordering::Acquire) != 0 { thread::yield_now(); }
    }
    pub fn resume(&self) {
        self.shared.paused.store(false, Ordering::Release);
        if self.shared.active.load(Ordering::Acquire) { if let Some(stream) = &self.stream { let _ = stream.play(); } }
    }
    pub fn toggle_pause(&self) { if self.shared.paused.load(Ordering::Acquire) { self.resume(); } else { self.pause(); } }
    pub fn is_paused(&self) -> bool { self.shared.paused.load(Ordering::Acquire) }
    pub fn seek(&mut self, seconds: f64) -> Result<(), String> {
        let path = self.path.clone().ok_or("No audio track is loaded")?;
        self.start(&path, seconds, self.is_paused(), Some(self.duration))
    }
    pub fn position(&self) -> f64 { self.shared.frames.load(Ordering::Acquire) as f64 / SAMPLE_RATE as f64 }
    pub fn duration(&self) -> f64 { self.duration }
    pub fn is_playing(&self) -> bool { self.shared.active.load(Ordering::Acquire) && !self.is_paused() }
    pub fn finished(&self) -> bool {
        let ended = self.shared.ended.load(Ordering::Acquire);
        // Called by the normal UI status tick; releases WASAPI while idle.
        if ended { if let Some(stream) = &self.stream { let _ = stream.pause(); } }
        ended
    }
    pub fn volume(&self) -> f32 { f32::from_bits(self.shared.volume.load(Ordering::Relaxed)) }
    /// Absolute 0..1 volume; values above one are interpreted as percentages.
    pub fn set_volume(&self, volume: f32) {
        if !volume.is_finite() { return; }
        let value = if volume > 1. { volume / 100. } else { volume };
        self.shared.volume.store(value.clamp(0., 1.).to_bits(), Ordering::Relaxed);
    }
    pub fn adjust_volume(&self, delta: f32) { self.set_volume((self.volume() + if delta > 1. { delta / 100. } else { delta }).clamp(0., 1.)); }
    pub fn set_gaming(&self, gaming: bool) { self.shared.gaming.store(gaming, Ordering::Release); }
    pub fn gaming(&self) -> bool { self.shared.gaming.load(Ordering::Acquire) }
    pub fn eq_gains(&self) -> [f32; 10] { self.shared.effects.lock().unwrap_or_else(|e| e.into_inner()).gains }
    pub fn set_eq_gain(&mut self, frequency: u32, db: f32) {
        if !db.is_finite() { return; }
        if let Some(index) = ISO_BANDS.iter().position(|f| *f == frequency) {
            self.shared.effects.lock().unwrap_or_else(|e| e.into_inner()).gains[index] = db.clamp(-12., 12.);
            self.eq_dirty = true;
        }
    }
    pub fn enable_echo(&self, delay_ms: f32, feedback: f32, wet: f32) {
        self.set_echo(delay_ms, feedback, wet);
        self.shared.effects.lock().unwrap_or_else(|e| e.into_inner()).echo_revision += 1;
    }
    pub fn set_echo(&self, delay_ms: f32, feedback: f32, wet: f32) {
        if [delay_ms, feedback, wet].iter().all(|v| v.is_finite()) { self.shared.effects.lock().unwrap_or_else(|e| e.into_inner()).echo = Some(EchoParams { delay_ms, feedback, wet }.clamped()); }
    }
    pub fn echo(&self) -> Option<EchoParams> { self.shared.effects.lock().unwrap_or_else(|e| e.into_inner()).echo }
    pub fn disable_echo(&self) {
        let mut effects = self.shared.effects.lock().unwrap_or_else(|e| e.into_inner());
        effects.echo = None; effects.echo_revision += 1;
    }
    pub fn underflows(&self) -> u64 { self.shared.underflows.load(Ordering::Relaxed) }
    pub fn buffered_seconds(&self) -> f64 { self.shared.ring.len() as f64 / SAMPLE_RATE as f64 }
    pub fn last_error(&self) -> Option<String> { self.shared.error.lock().unwrap_or_else(|e| e.into_inner()).clone() }
    pub fn shutdown(&mut self) {
        self.stop(); self.stream.take();
        if self.eq_dirty {
            let gains: std::collections::BTreeMap<String, f32> = ISO_BANDS.iter().zip(self.eq_gains()).map(|(band, gain)| (band.to_string(), gain)).collect();
            if let Ok(data) = serde_json::to_vec(&gains) { let _ = std::fs::write(self.base.join("musicapp_eq.json"), data); }
            self.eq_dirty = false;
        }
    }
}
impl Drop for AudioEngine { fn drop(&mut self) { self.shutdown(); } }

fn build_stream<T>(device: &cpal::Device, config: &cpal::StreamConfig, shared: Arc<Shared>) -> Result<cpal::Stream, String>
where T: cpal::SizedSample + cpal::FromSample<f32> {
    let channels = config.channels as usize;
    let ratio = SAMPLE_RATE as f64 / config.sample_rate.0 as f64;
    let mut renderer = Renderer::default();
    let error_shared = shared.clone();
    device.build_output_stream(config, move |out: &mut [T], _| {
        struct CallbackGuard<'a>(&'a AtomicUsize);
        impl Drop for CallbackGuard<'_> { fn drop(&mut self) { self.0.fetch_sub(1, Ordering::Release); } }
        shared.callbacks.fetch_add(1, Ordering::Acquire);
        let _callback_guard = CallbackGuard(&shared.callbacks);
        let generation = shared.generation.load(Ordering::Acquire);
        if renderer.generation != generation { renderer = Renderer { generation, ..Renderer::default() }; }
        if !shared.active.load(Ordering::Acquire) || shared.paused.load(Ordering::Acquire) { out.fill(T::from_sample(0.)); return; }
        let volume = f32::from_bits(shared.volume.load(Ordering::Relaxed));
        let mut consumed = 0;
        let mut starving = false;
        for frame in out.chunks_mut(channels) {
            let sample = if ratio == 1. {
                match shared.ring.pop() { Some(v) => { consumed += 1; v }, None => { starving = true; [0.; 2] } }
            } else { renderer.next(&shared.ring, ratio, &mut consumed, &mut starving) };
            for (ch, output) in frame.iter_mut().enumerate() {
                let value = if channels == 1 { (sample[0] + sample[1]) * 0.5 } else if ch < 2 { sample[ch] } else { 0. };
                *output = T::from_sample(value * volume);
            }
        }
        shared.frames.fetch_add(consumed, Ordering::Relaxed);
        if starving {
            if shared.decoded.load(Ordering::Acquire) && shared.ring.len() == 0 && renderer.current.is_none() {
                shared.active.store(false, Ordering::Release);
                shared.ended.store(true, Ordering::Release);
            } else { shared.underflows.fetch_add(1, Ordering::Relaxed); }
        }
    }, move |error| {
        *error_shared.error.lock().unwrap_or_else(|e| e.into_inner()) = Some(format!("Audio device: {error}"));
        error_shared.active.store(false, Ordering::Release);
    }, None).map_err(|e| e.to_string())
}

#[derive(Default)]
struct Renderer { current: Option<[f32; 2]>, next: Option<[f32; 2]>, phase: f64, generation: u64 }
impl Renderer {
    fn next(&mut self, ring: &FrameRing, ratio: f64, consumed: &mut u64, starving: &mut bool) -> [f32; 2] {
        if self.current.is_none() { self.current = ring.pop(); }
        let Some(current) = self.current else { *starving = true; return [0.; 2]; };
        if self.next.is_none() { self.next = ring.pop(); }
        let next = self.next.unwrap_or([0.; 2]);
        let out = [current[0] + (next[0] - current[0]) * self.phase as f32, current[1] + (next[1] - current[1]) * self.phase as f32];
        self.phase += ratio;
        while self.phase >= 1. {
            *consumed += 1;
            self.phase -= 1.;
            self.current = self.next.take();
            if self.current.is_none() { self.phase = 0.; *starving = true; break; }
            self.next = ring.pop();
        }
        out
    }
}

fn decode(mut stdout: impl Read, shared: Arc<Shared>, cancel: Arc<AtomicBool>, process: Arc<Mutex<Option<Child>>>) {
    let mut bytes = [0u8; CHUNK * 8];
    let mut pcm = [[0f32; 2]; CHUNK];
    let mut eq = Equalizer::new(SAMPLE_RATE, CHUNK);
    let mut echo = Echo::new(SAMPLE_RATE);
    let mut echo_revision = 0;
    let mut previous_gaming = true;
    'decode: loop {
        if cancel.load(Ordering::Acquire) { break; }
        let mut filled = 0;
        while filled < bytes.len() {
            match stdout.read(&mut bytes[filled..]) {
                Ok(0) => break,
                Ok(count) => filled += count,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => { if !cancel.load(Ordering::Acquire) { *shared.error.lock().unwrap_or_else(|e| e.into_inner()) = Some(format!("Audio decode pipe: {e}")); } break; }
            }
        }
        let frames = filled / 8;
        if frames == 0 { break; }
        for (frame, bytes) in pcm[..frames].iter_mut().zip(bytes.chunks_exact(8)) {
            frame[0] = f32::from_le_bytes(bytes[..4].try_into().unwrap());
            frame[1] = f32::from_le_bytes(bytes[4..8].try_into().unwrap());
        }
        let gaming=shared.gaming.load(Ordering::Acquire);
        if gaming!=previous_gaming {eq.reset();echo.reset();previous_gaming=gaming;}
        if !gaming {
            let effects = *shared.effects.lock().unwrap_or_else(|e| e.into_inner());
            eq.process(&mut pcm[..frames], effects.gains);
            if echo_revision != effects.echo_revision { echo.reset(); echo_revision = effects.echo_revision; }
            if let Some(params) = effects.echo { echo.process(&mut pcm[..frames], params); } else { echo.reset(); }
        }
        for frame in &pcm[..frames] {
            while !shared.ring.push(*frame) {
                if cancel.load(Ordering::Acquire) { break 'decode; }
                // Blocks only the producer; callback contains no sleeps or locks.
                // A half-second ring tolerates this 10ms producer wait and avoids
                // hundreds of unnecessary wakeups per second while it is full.
                thread::park_timeout(Duration::from_millis(10));
            }
        }
        if filled < bytes.len() { break; }
    }
    if !cancel.load(Ordering::Acquire) {
        if let Some(child) = process.lock().unwrap_or_else(|e| e.into_inner()).as_mut() {
            if let Ok(status) = child.wait() { if !status.success() { *shared.error.lock().unwrap_or_else(|e| e.into_inner()) = Some(format!("FFmpeg decoding exited with {status}")); } }
        }
    }
    shared.decoded.store(true, Ordering::Release);
}

/// Hann-window FFT overlap-add, preserving the Python implementation's one-hop delay.
pub struct Equalizer {
    sample_rate: u32, hop: usize, gains: Option<[f32; 10]>,
    forward: Option<Arc<dyn Fft<f32>>>, inverse: Option<Arc<dyn Fft<f32>>>,
    window: Vec<f32>, input: Vec<[f32; 2]>, overlap: Vec<[f32; 2]>,
    spectrum: Vec<Complex32>, scratch: Vec<Complex32>, curve: Vec<f32>, output: Vec<[f32; 2]>,
}
impl Equalizer {
    pub fn new(sample_rate: u32, hop: usize) -> Self { Self { sample_rate, hop, gains: None, forward: None, inverse: None, window: vec![], input: vec![], overlap: vec![], spectrum: vec![], scratch: vec![], curve: vec![], output: vec![] } }
    fn initialize(&mut self) {
        let size = self.hop * 2;
        let mut planner = FftPlanner::<f32>::new();
        self.forward = Some(planner.plan_fft_forward(size));
        self.inverse = Some(planner.plan_fft_inverse(size));
        self.window = (0..size).map(|i| (0.5 - 0.5 * (std::f64::consts::TAU * i as f64 / size as f64).cos()) as f32).collect();
        self.input = vec![[0.; 2]; size]; self.overlap = vec![[0.; 2]; self.hop]; self.output = vec![[0.; 2]; self.hop];
        self.spectrum = vec![Complex32::default(); size];
        self.scratch = vec![Complex32::default(); self.forward.as_ref().unwrap().get_inplace_scratch_len().max(self.inverse.as_ref().unwrap().get_inplace_scratch_len())];
        self.curve = vec![1.; size];
    }
    pub fn reset(&mut self) { self.input.fill([0.; 2]); self.overlap.fill([0.; 2]); }
    fn rebuild_curve(&mut self, gains: [f32; 10]) {
        let nyquist = self.sample_rate as f64 / 2.;
        let values: Vec<f64> = gains.iter().map(|g| 10f64.powf(*g as f64 / 20.)).collect();
        let mut points = vec![(0f64, values[0])];
        for (i, band) in ISO_BANDS.iter().enumerate() { if (*band as f64) < nyquist { points.push((*band as f64, values[i])); } }
        points.push((nyquist, points.last().unwrap().1));
        for i in 0..=self.hop {
            let frequency = i as f64 * self.sample_rate as f64 / (self.hop * 2) as f64;
            let right = points.iter().position(|(f, _)| *f >= frequency).unwrap_or(points.len() - 1);
            let left = right.saturating_sub(1);
            let (fa, a) = points[left]; let (fb, b) = points[right];
            let gain = if left == right { a } else { let factor = ((frequency + 1e-6).log10() - (fa + 1e-6).log10()) / ((fb + 1e-6).log10() - (fa + 1e-6).log10()); a + (b - a) * factor } as f32;
            self.curve[i] = gain;
            if i > 0 && i < self.hop { self.curve[self.hop * 2 - i] = gain; }
        }
        self.gains = Some(gains);
    }
    pub fn process(&mut self, pcm: &mut [[f32; 2]], gains: [f32; 10]) {
        if pcm.is_empty() { return; }
        if gains.iter().all(|g| *g == 0.) { self.reset(); return; }
        assert!(pcm.len() <= self.hop, "EQ input exceeds a decoder hop");
        if self.forward.is_none() { self.initialize(); }
        if self.gains != Some(gains) { self.rebuild_curve(gains); }
        let size = self.hop * 2;
        self.input.copy_within(self.hop.., 0);
        self.input[self.hop..].fill([0.; 2]);
        self.input[self.hop..self.hop + pcm.len()].copy_from_slice(pcm);
        for channel in 0..2 {
            for i in 0..size { self.spectrum[i] = Complex32::new(self.input[i][channel] * self.window[i], 0.); }
            self.forward.as_ref().unwrap().process_with_scratch(&mut self.spectrum, &mut self.scratch);
            for (value, gain) in self.spectrum.iter_mut().zip(&self.curve) { *value *= *gain; }
            self.inverse.as_ref().unwrap().process_with_scratch(&mut self.spectrum, &mut self.scratch);
            for i in 0..self.hop {
                self.output[i][channel] = (self.spectrum[i].re / size as f32 + self.overlap[i][channel]).clamp(-1., 1.);
                self.overlap[i][channel] = self.spectrum[i + self.hop].re / size as f32;
            }
        }
        pcm.copy_from_slice(&self.output[..pcm.len()]);
    }
}

pub struct Echo { sample_rate: u32, buffer: Vec<[f32; 2]>, index: usize }
impl Echo {
    pub fn new(sample_rate: u32) -> Self { Self { sample_rate, buffer: vec![], index: 0 } }
    pub fn reset(&mut self) { self.buffer.fill([0.; 2]); self.index = 0; }
    pub fn process(&mut self, pcm: &mut [[f32; 2]], params: EchoParams) {
        let params = params.clamped();
        if params.wet == 0. || pcm.is_empty() { return; }
        let length = ((self.sample_rate as f64 * params.delay_ms as f64 / 1000.) as usize).max(1);
        if self.buffer.len() != length { self.buffer = vec![[0.; 2]; length]; self.index = 0; }
        for frame in pcm {
            let delayed = self.buffer[self.index];
            for channel in 0..2 {
                self.buffer[self.index][channel] = frame[channel] + delayed[channel] * params.feedback;
                frame[channel] = frame[channel] * (1. - params.wet) + delayed[channel] * params.wet;
            }
            self.index = (self.index + 1) % length;
        }
    }
}

fn valid_duration(duration: f64) -> Option<f64> {
    (duration.is_finite() && duration > 0.).then_some(duration)
}
fn resolve_duration(
    hint: Option<f64>, cached: Option<f64>,
    metadata: impl FnOnce() -> Option<f64>, fallback: impl FnOnce() -> Option<f64>,
) -> f64 {
    hint.and_then(valid_duration)
        .or_else(|| cached.and_then(valid_duration))
        .or_else(|| metadata().and_then(valid_duration))
        .or_else(|| fallback().and_then(valid_duration)).unwrap_or(0.)
}
fn local_duration(path: &str) -> Option<f64> {
    if path.starts_with("http://") || path.starts_with("https://") { return None; }
    use lofty::{config::ParseOptions, file::AudioFile, probe::Probe};
    let options = ParseOptions::new().read_tags(false).read_cover_art(false);
    let file = Probe::open(path).ok()?.guess_file_type().ok()?.options(options).read().ok()?;
    valid_duration(file.properties().duration().as_secs_f64())
}

#[derive(Clone, PartialEq, Eq)]
struct ToolKey {
    base: PathBuf, name: String,
    configured: Option<std::ffi::OsString>,
    ffmpeg: Option<std::ffi::OsString>,
    search_path: Option<std::ffi::OsString>,
}
#[derive(Default)]
struct ToolCache { entries: Vec<(ToolKey, PathBuf)> }
impl ToolCache {
    fn get(&self, key: &ToolKey) -> Option<PathBuf> {
        self.entries.iter().find(|(cached, path)| cached == key && path.is_file()).map(|(_, path)| path.clone())
    }
    fn insert(&mut self, key: ToolKey, path: PathBuf) {
        self.entries.retain(|(cached, _)| cached != &key);
        if self.entries.len() == 8 { self.entries.remove(0); }
        self.entries.push((key, path));
    }
}
pub fn find_tool(base: &Path, name: &str) -> PathBuf {
    static CACHE: OnceLock<Mutex<ToolCache>> = OnceLock::new();
    static EXECUTABLE: OnceLock<Option<PathBuf>> = OnceLock::new();
    let exe = format!("{name}.exe");
    let configured = std::env::var_os(format!("MUSICAPP_{}", name.to_ascii_uppercase()));
    let ffmpeg = (name == "ffprobe").then(|| std::env::var_os("MUSICAPP_FFMPEG")).flatten();
    // The app supplies an exact path. Keep this common path to one existence
    // check, with no directory walk or global-cache lock.
    if let Some(configured) = &configured {
        let path = PathBuf::from(configured);
        if path.is_file() { return path; }
        if path.is_dir() { let candidate = path.join(&exe); if candidate.is_file() { return candidate; } }
    }
    if let Some(configured) = &ffmpeg {
            let path = PathBuf::from(configured);
            let sibling = if path.is_dir() { path.join(&exe) } else { path.with_file_name(&exe) };
            if sibling.is_file() { return sibling; }
    }
    let key = ToolKey { base: base.to_path_buf(), name: name.into(), configured, ffmpeg, search_path: std::env::var_os("PATH") };
    let cache = CACHE.get_or_init(|| Mutex::new(ToolCache::default()));
    if let Some(path) = cache.lock().unwrap_or_else(|e| e.into_inner()).get(&key) { return path; }
    let mut roots = vec![base.join("tools"), base.join("ffmpeg-bin"), base.parent().unwrap_or(base).join("tools"), base.parent().unwrap_or(base).join("ffmpeg-bin")];
    if let Some(executable) = EXECUTABLE.get_or_init(|| std::env::current_exe().ok()) {
        if let Some(directory) = executable.parent() {
            roots.push(directory.join("tools")); roots.push(directory.join("ffmpeg-bin"));
        }
        // Development builds live in target/release, packages beside ui/tools.
        if let Some(root) = executable.ancestors().skip(1).take(4).find(|root| root.join("ui/app.tcl").is_file()) {
            roots.push(root.join("tools")); roots.push(root.join("ffmpeg-bin"));
        }
    }
    roots.dedup();
    let mut found = roots.iter().map(|root| root.join(&exe)).find(|path| path.is_file());
    if found.is_none() {
        let mut budget = 128;
        let mut recursive_roots = roots.clone();
        recursive_roots.sort_by_key(|root| if root.file_name().is_some_and(|name| name == "ffmpeg-bin") { 0 } else { 1 });
        for root in &recursive_roots {
            if let Some(path) = find_in(root, &exe, 3, &mut budget) { found = Some(path); break; }
        }
    }
    if found.is_none() {
        // Only the native executable name is searched. Never use PATHEXT,
        // Python, batch wrappers, or an interpreter as a decoder fallback.
        found = key.search_path.as_ref().and_then(|path| std::env::split_paths(path).map(|directory| directory.join(&exe)).find(|path| path.is_file()));
    }
    if let Some(path) = found {
        cache.lock().unwrap_or_else(|e| e.into_inner()).insert(key, path.clone());
        return path;
    }
    // Do not cache misses; a subsequent project-local setup can install tools.
    base.join("tools").join(exe)
}
fn find_in(directory: &Path, filename: &str, depth: usize, budget: &mut usize) -> Option<PathBuf> {
    let candidate = directory.join(filename);
    if candidate.is_file() { return Some(candidate); }
    if depth == 0 || *budget == 0 { return None; }
    for entry in std::fs::read_dir(directory).ok()?.flatten() {
        if *budget == 0 { break; } *budget -= 1;
        if let Ok(kind) = entry.file_type() {
            if kind.is_dir() && !kind.is_symlink() {
                if let Some(path) = find_in(&entry.path(), filename, depth - 1, budget) { return Some(path); }
            }
        }
    }
    None
}
fn hidden_command(executable: &Path) -> Command {
    let mut command = Command::new(executable);
    #[cfg(windows)] { use std::os::windows::process::CommandExt; command.creation_flags(0x08000000); }
    command
}
fn probe_duration(ffprobe: &Path, path: &str) -> Option<f64> {
    let mut command = hidden_command(ffprobe);
    command.args(["-v", "error", "-rw_timeout", "3000000", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]);
    let mut child = command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().ok()?;
    let deadline = Instant::now() + Duration::from_secs(4);
    loop {
        if child.try_wait().ok()?.is_some() { break; }
        if Instant::now() >= deadline { let _ = child.kill(); let _ = child.wait(); return None; }
        thread::sleep(Duration::from_millis(5));
    }
    let mut output = String::new(); child.stdout.take()?.read_to_string(&mut output).ok()?;
    output.trim().parse::<f64>().ok().filter(|v| v.is_finite() && *v >= 0.)
}

// Imageio's bundled FFmpeg does not include ffprobe. Its input metadata is
// sufficient for local duration; this command never decodes the whole track.
fn probe_ffmpeg_duration(ffmpeg: &Path, path: &str) -> Option<f64> {
    let mut command = hidden_command(ffmpeg);
    command.args(["-nostdin", "-hide_banner", "-rw_timeout", "3000000", "-i", path]);
    let mut child = command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped()).spawn().ok()?;
    let deadline = Instant::now() + Duration::from_secs(4);
    loop {
        if child.try_wait().ok()?.is_some() { break; }
        if Instant::now() >= deadline { let _ = child.kill(); let _ = child.wait(); break; }
        thread::sleep(Duration::from_millis(5));
    }
    let mut metadata = String::new(); child.stderr.take()?.read_to_string(&mut metadata).ok()?;
    parse_ffmpeg_duration(&metadata)
}
fn parse_ffmpeg_duration(metadata: &str) -> Option<f64> {
    let time = metadata.split("Duration:").nth(1)?.split(',').next()?.trim();
    let values = time.split(':').map(str::parse::<f64>).collect::<Result<Vec<_>, _>>().ok()?;
    if values.len() != 3 { return None; }
    let duration = values[0] * 3600. + values[1] * 60. + values[2];
    (duration.is_finite() && duration >= 0.).then_some(duration)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn supplied_duration_avoids_all_metadata_and_probe_work() {
        let duration = resolve_duration(Some(123.456), Some(99.),
            || panic!("A library duration must not reopen local metadata"),
            || panic!("A library duration must not spawn a probe process"));
        assert_eq!(duration, 123.456);
        assert_eq!(resolve_duration(None, Some(87.), || panic!("Cached seek duration reparsed metadata"), || panic!("Cached seek duration spawned a probe")), 87.);
    }
    #[test]
    fn invalid_duration_hints_fall_back_to_native_metadata_then_probe() {
        for hint in [f64::NAN, f64::INFINITY, -1., 0.] {
            assert_eq!(resolve_duration(Some(hint), None, || Some(6.25), || panic!("Native metadata should avoid probes")), 6.25);
        }
        assert_eq!(resolve_duration(None, None, || Some(0.), || Some(12.5)), 12.5);
        assert_eq!(resolve_duration(None, None, || None, || Some(f64::NAN)), 0.);
    }
    #[test]
    fn binary_cache_is_bounded_and_does_not_return_deleted_files() {
        let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests").join(format!(".tool-cache-{}-{}", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        std::fs::write(&fixture, b"cache lookup fixture; never executed").unwrap();
        let key = |index| ToolKey { base: PathBuf::new(), name: format!("tool-{index}"), configured: None, ffmpeg: None, search_path: None };
        let mut cache = ToolCache::default();
        for index in 0..20 { cache.insert(key(index), fixture.clone()); }
        assert_eq!(cache.entries.len(), 8);
        assert!(cache.get(&key(0)).is_none()); assert_eq!(cache.get(&key(19)), Some(fixture.clone()));
        std::fs::remove_file(&fixture).unwrap();
        assert!(cache.get(&key(19)).is_none());
    }
    #[test]
    fn ring_wraps_without_losing_order_and_stays_bounded() {
        let ring = FrameRing::new(3);
        for i in 0..3 { assert!(ring.push([i as f32, -(i as f32)])); }
        assert!(!ring.push([99.; 2])); assert_eq!(ring.pop(), Some([0., 0.]));
        assert!(ring.push([3., -3.]));
        for i in 1..4 { assert_eq!(ring.pop(), Some([i as f32, -(i as f32)])); }
        assert_eq!(ring.pop(), None); assert_eq!(ring.len(), 0);
    }
    #[test]
    fn ring_cross_thread_preserves_all_frames() {
        let ring = Arc::new(FrameRing::new(71)); let writer = ring.clone();
        let worker = thread::spawn(move || { for i in 0..100_000 { while !writer.push([i as f32, -(i as f32)]) { thread::yield_now(); } } });
        for i in 0..100_000 { loop { if let Some(frame) = ring.pop() { assert_eq!(frame, [i as f32, -(i as f32)]); break; } thread::yield_now(); } }
        worker.join().unwrap();
    }
    #[test]
    fn echo_matches_python_impulse_across_block_boundaries() {
        let mut echo = Echo::new(1000); let params = EchoParams { delay_ms: 10., feedback: 0.5, wet: 0.5 };
        let mut pcm = [[0.; 2]; 31]; pcm[0] = [1.; 2];
        echo.process(&mut pcm[..7], params); echo.process(&mut pcm[7..18], params); echo.process(&mut pcm[18..], params);
        for (i, frame) in pcm.iter().enumerate() { assert_eq!(*frame, [match i { 0 | 10 => 0.5, 20 => 0.25, 30 => 0.125, _ => 0. }; 2]); }
        echo.reset(); let mut silent = [[0.; 2]; 10]; echo.process(&mut silent, params); assert_eq!(silent, [[0.; 2]; 10]);
    }
    #[test]
    fn flat_eq_is_bit_exact_and_never_initializes_fft() {
        let mut eq = Equalizer::new(44_100, 64);
        let mut pcm = std::array::from_fn::<_, 64, _>(|i| [i as f32 / 128., -(i as f32) / 128.]); let before = pcm;
        eq.process(&mut pcm, [0.; 10]); assert_eq!(pcm, before); assert!(eq.forward.is_none());
    }
    #[test]
    fn active_eq_has_python_hop_delay_clipping_and_partial_blocks() {
        let mut eq = Equalizer::new(16_000, 64); let mut gains = [12.; 10]; gains[5] = -12.;
        let mut block = [[0.2; 2]; 64]; eq.process(&mut block, gains);
        for frame in block { assert!(frame[0].is_finite() && frame[0].abs() <= 1.); assert_eq!(frame[0], frame[1]); }
        let mut partial = [[0.2; 2]; 17]; eq.process(&mut partial, gains); assert!(partial.iter().any(|v| v[0].abs() > 0.));
        eq.reset(); let mut zero = [[0.; 2]; 64]; eq.process(&mut zero, gains); assert!(zero.iter().all(|v| v[0].abs() < 1e-6));
    }
    #[test]
    fn eq_matches_original_python_golden_samples() {
        #[derive(Deserialize)] struct Block { input: Vec<[f32; 2]>, expected: Vec<[f32; 2]> }
        #[derive(Deserialize)] struct Golden { sample_rate: u32, hop: usize, gains: [f32; 10], blocks: Vec<Block> }
        let golden: Golden = serde_json::from_str(include_str!("../tests/fixtures/audio_eq_python.json")).unwrap();
        let mut eq = Equalizer::new(golden.sample_rate, golden.hop);
        for mut block in golden.blocks {
            eq.process(&mut block.input, golden.gains);
            for (actual, expected) in block.input.iter().flatten().zip(block.expected.iter().flatten()) {
                assert!((actual - expected).abs() < 5e-6, "Python DSP differs: Rust={actual}, Python={expected}");
            }
        }
    }
    #[test]
    fn resampling_consumes_every_source_frame_once() {
        for ratio in [44_100. / 48_000., 44_100. / 96_000., 44_100. / 32_000.] {
            let ring = FrameRing::new(100); for i in 0..100 { assert!(ring.push([i as f32 / 100.; 2])); }
            let mut renderer = Renderer::default(); let mut consumed = 0; let mut starving = false;
            for _ in 0..400 { renderer.next(&ring, ratio, &mut consumed, &mut starving); }
            assert_eq!(consumed, 100); assert_eq!(ring.len(), 0); assert!(starving);
        }
    }
    #[test]
    fn ffmpeg_duration_parser_handles_hours_and_live_unknown() {
        assert_eq!(parse_ffmpeg_duration("Input\n  Duration: 01:02:03.45, start: 0.0"), Some(3723.45));
        assert_eq!(parse_ffmpeg_duration("Duration: N/A, start: 0"), None);
    }
    #[test]
    fn configured_ffmpeg_resolves_outside_state_directory() {
        let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let binary = base.join("tools").join("ffmpeg.exe");
        if !binary.is_file() { return; }
        let previous = std::env::var_os("MUSICAPP_FFMPEG");
        std::env::set_var("MUSICAPP_FFMPEG", &binary);
        assert_eq!(find_tool(&base.join("tests").join("unrelated-state"), "ffmpeg"), binary);
        std::env::set_var("MUSICAPP_FFMPEG", binary.parent().unwrap());
        assert_eq!(find_tool(&base.join("tests").join("unrelated-state"), "ffmpeg"), binary);
        if let Some(value) = previous { std::env::set_var("MUSICAPP_FFMPEG", value); } else { std::env::remove_var("MUSICAPP_FFMPEG"); }
    }
    #[test]
    fn actual_decoder_resamples_mono_and_seeks_without_device() {
        let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let ffmpeg = find_tool(&base, "ffmpeg");
        // Decoder fixtures are created exclusively inside the Rust project.
        let fixture = base.join("tests").join(format!(".audio-{}.wav", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        let samples = 28_800usize;
        let mut bytes = Vec::new();
        bytes.extend_from_slice(b"RIFF"); bytes.extend_from_slice(&(36u32 + samples as u32 * 2).to_le_bytes()); bytes.extend_from_slice(b"WAVEfmt ");
        bytes.extend_from_slice(&16u32.to_le_bytes()); bytes.extend_from_slice(&1u16.to_le_bytes()); bytes.extend_from_slice(&1u16.to_le_bytes());
        bytes.extend_from_slice(&48_000u32.to_le_bytes()); bytes.extend_from_slice(&96_000u32.to_le_bytes()); bytes.extend_from_slice(&2u16.to_le_bytes()); bytes.extend_from_slice(&16u16.to_le_bytes());
        bytes.extend_from_slice(b"data"); bytes.extend_from_slice(&(samples as u32 * 2).to_le_bytes());
        for i in 0..samples { bytes.extend_from_slice(&(if i < 14_400 { 8192i16 } else { -8192i16 }).to_le_bytes()); }
        std::fs::write(&fixture, bytes).unwrap();
        struct RemoveFile(PathBuf); impl Drop for RemoveFile { fn drop(&mut self) { let _ = std::fs::remove_file(&self.0); } }
        let _cleanup = RemoveFile(fixture.clone());
        assert!((local_duration(fixture.to_str().unwrap()).expect("Native WAV metadata") - 0.6).abs() < 1e-6);
        assert_eq!(resolve_duration(None, None, || local_duration(fixture.to_str().unwrap()), || panic!("Readable WAV must not spawn a probe")), 0.6);
        assert!((probe_ffmpeg_duration(&ffmpeg, fixture.to_str().unwrap()).expect("FFmpeg must be available for integration test") - 0.6).abs() < 0.001);
        let mut child = hidden_command(&ffmpeg).args(["-nostdin", "-v", "error", "-ss", "0.4", "-i", fixture.to_str().unwrap(), "-threads", "1", "-ac", "2", "-ar", "44100", "-f", "f32le", "pipe:1"]).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap();
        let stdout = child.stdout.take().unwrap();
        let shared = Arc::new(Shared::new());
        let process = Arc::new(Mutex::new(Some(child)));
        decode(stdout, shared.clone(), Arc::new(AtomicBool::new(false)), process);
        assert!(shared.decoded.load(Ordering::Acquire)); assert_eq!(shared.ring.len(), 8820);
        while let Some(frame) = shared.ring.pop() { assert!((frame[0] - frame[1]).abs() < 1e-6); assert!(frame[0] < -0.17 && frame[0] > -0.26, "{frame:?}"); }
    }
}
