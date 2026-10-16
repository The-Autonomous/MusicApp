# Third-party components

The original MusicApp source license remains unchanged in the original project.
This Rust application's aggregate package license is `GPL-3.0-only` because it
links RustyPipe. Original MIT-licensed work retains its original license notice.

## RustyPipe 0.11.4

- Author: ThetaDev.
- Upstream SPDX metadata: `GPL-3.0` (the upstream crate's exact identifier).
- Aggregate application metadata uses `GPL-3.0-only`.
- Full upstream license: [licenses/rustypipe-GPL-3.0.md](licenses/rustypipe-GPL-3.0.md).
- Source: <https://codeberg.org/ThetaDev/rustypipe>.
- Exact crate source: <https://crates.io/api/v1/crates/rustypipe/0.11.4/download>.
- Documentation: <https://docs.rs/rustypipe/0.11.4/rustypipe/>.

RustyPipe provides native YouTube/YouTube Music API parsing and player stream
extraction. Its external Botguard executable discovery is explicitly disabled.
The application does not invoke a script-based downloader.

## Other direct Rust dependencies

Exact selected versions and source checksums are recorded in `Cargo.lock`.
Their full upstream source and license files can be obtained with `cargo vendor`
when preparing a source distribution.

| Component | Purpose | Upstream license metadata |
|---|---|---|
| CPAL | Native Windows audio output | Apache-2.0 |
| RustFFT | Equalizer FFT processing | MIT OR Apache-2.0 |
| Serde / serde_json | Local state and native API messages | MIT OR Apache-2.0 |
| Regex | Text, title, and lyric parsing | MIT OR Apache-2.0 |
| unicode-normalization | Song identity normalization | MIT OR Apache-2.0 |
| URL | URL parsing and validation | MIT OR Apache-2.0 |
| libloading | Native library loading | ISC |
| cesu8 | Tcl string conversion | Apache-2.0 OR MIT |
| Rand | Shuffle and recommendation selection | MIT OR Apache-2.0 |
| Reqwest | Native HTTP | MIT OR Apache-2.0 |
| tiny_http | Native radio and phone-control HTTP | MIT OR Apache-2.0 |
| Lofty | Native audio metadata | MIT OR Apache-2.0 |
| Tokio | Native asynchronous networking | MIT |
| Tungstenite | Local browser DevTools WebSocket | MIT OR Apache-2.0 |

## Native runtime components

Tcl/Tk supplies the existing native rendering engine. FFmpeg supplies native
audio decoding/encoding. These are native runtime components. Tcl evaluates the UI presentation definitions; application services run in Rust. Their applicable upstream license texts and corresponding
source must accompany a redistributed runtime according to the exact builds
included. FFmpeg's license depends on its build configuration; inspect the
included executable with `ffmpeg -L` and `ffmpeg -buildconf`.

This notice is an inventory, not a replacement for the upstream license texts.

The retired Firefox/SQLite fallback is archived outside the active port and is
not a runtime dependency. No Python DLL or runtime is loaded.
The Rust package's FFmpeg is a custom FFmpeg 7.1.5 audio build, with static LAME
3.100 and zlib 1.3.2, Windows Schannel HTTPS and x86 SIMD dispatch. Its own license
is LGPL version 2.1 or later. Exact build/license output is in
`licenses/FFmpeg-build.txt`; full notices are in `licenses/FFmpeg-LGPL-2.1.txt`,
`licenses/LAME-LGPL.txt` and `licenses/zlib-LICENSE.txt`.
Corresponding unmodified sources and archive checksums are retained in
`native/ffmpeg/downloads`, `native/ffmpeg/sources.json` and the build scripts in
`native/ffmpeg`. The previous gyan.dev binary and its notice remain separately
preserved under `native/ffmpeg/reference`; the original Python installation is
unchanged.
Native upstreams: <https://ffmpeg.org/download.html>,
<https://lame.sourceforge.io/>, <https://zlib.net/>,
<https://www.tcl-lang.org/software/tcltk/>.
