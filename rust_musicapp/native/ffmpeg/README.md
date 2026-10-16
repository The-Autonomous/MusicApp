# MusicApp audio-only FFmpeg

This native build is separate from the original Python application and its
FFmpeg. `Build-FFmpeg.ps1` builds a candidate; it does not deploy it. Compatibility
validation must pass before copying the candidate into the Rust app's `tools`.

Build requirements: existing Visual Studio 2022 x64 C tools, its native CMake,
MSYS2 base shell/runtime, Git's Unix utilities, portable GNU Make 4.4.1 and NASM
2.16.03. The default compiler is the existing LLVM clang-cl 20.1.7; pass
`-Compiler msvc` to compare the Microsoft compiler. All downloaded
sources, portable tools, build products and temporary files stay in this folder.
The scripts do not install or upgrade system tools. Run from PowerShell:

```powershell
.\Build-FFmpeg.ps1
```

The source versions are FFmpeg 7.1.5, LAME 3.100 and zlib 1.3.2. Static linking
keeps LAME and zlib inside the executable. HTTPS uses Windows Schannel and the
Windows trust store. The build keeps FFmpeg's runtime-dispatched x86 SIMD and
speed optimization; it does not use size optimization at the expense of speed.
Native Windows system libraries are still required. No Python, Node, MSYS2 or
compiler runtime is launched by the resulting FFmpeg.

`audio-decoders.txt` records the native audio/PCM/DPCM/ADPCM decoder names from
upstream `libavcodec/allcodecs.c`, excluding the HDR image decoder placed in that
upstream section. Configuration can exclude decoders whose own
dependencies are disabled. The final `-decoders` inventory is the authority.
Demuxers cover MP3, WAV, Ogg, FLAC, MP4/M4A/MOV, WebM/Matroska, AAC and HTTP HLS
audio. Video tracks can be skipped without decoding them. Video/GPU decoding,
screen capture, video conversion and arbitrary FFmpeg CLI uses are outside this
application-specific build. MP3 encoding, audio resampling, SponsorBlock filters,
HTTP ranges and HTTPS remain included.

Reference binary: `reference/ffmpeg-7.1-full.exe` (the previously bundled 7.1
essentials release). Default Clang candidate: `prefix/bin/ffmpeg-clang.exe`;
Microsoft compiler comparison: `prefix/bin/ffmpeg.exe`. Logs and upstream
configuration are retained under `logs` and `build/ffmpeg-clang/ffbuild`.

Compatibility gate (Python is development-only, never packaged or used by app):

```powershell
python benchmarks/verify_slim_ffmpeg.py --reference native/ffmpeg/reference/ffmpeg-7.1-full.exe --candidate native/ffmpeg/prefix/bin/ffmpeg-clang.exe --output benchmarks/results/slim-ffmpeg-compatibility
```

Archives and hashes are listed in `sources.json`. For distributing this native
binary, keep its exact corresponding sources, these build scripts/configuration
and the supplied license notices available. The whole Rust app already includes
GPLv3 components; this native build's own licensing is recorded by `ffmpeg -L`.
