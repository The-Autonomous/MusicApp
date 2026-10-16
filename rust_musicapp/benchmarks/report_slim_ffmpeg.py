"""Assemble the final native FFmpeg evidence without running measurements."""
from pathlib import Path
import html
import json
import shutil
import zipfile

BASE = Path(__file__).resolve().parents[1]
RESULTS = BASE / 'benchmarks/results'
DEST = RESULTS / 'ffmpeg-audio-delivery'


def load(relative):
    return json.loads((RESULTS / relative).read_text(encoding='utf-8-sig'))


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '|' + '|'.join('---' for _ in headers) + '|', *['| ' + ' | '.join(map(str, row)) + ' |' for row in rows]])


def main():
    short = load('ffmpeg-audio-final-performance/benchmark.json')
    long = load('ffmpeg-audio-long-performance/benchmark.json')
    compat = load('ffmpeg-audio-final-compat/compatibility.json')
    verified = load('ffmpeg-audio-final-smoke/final-verification.json')
    expected_hash = short['executables']['Candidate']['sha256'].upper()
    assert expected_hash == long['executables']['Candidate']['sha256'].upper() == verified['native_ffmpeg_sha256']
    assert compat['candidate']['sha256'].upper() == expected_hash
    assert compat['passed'] and compat['passed_tests'] == 153 and not compat['failed_tests']
    assert len(short['runs']) == 134 and len(long['runs']) == 62 and not short['errors'] and not long['errors']
    DEST.mkdir(exist_ok=True)
    old_size = short['executables']['Reference']['size_bytes']
    new_size = short['executables']['Candidate']['size_bytes']
    startup_rows, long_rows = [], []
    for phase, data, target in [('startup', short, startup_rows), ('sustained', long, long_rows)]:
        codecs = dict.fromkeys(row['codec'] for row in data['summary'] if row['phase'] == phase)
        for codec in codecs:
            old = next(row for row in data['summary'] if row['phase'] == phase and row['codec'] == codec and row['implementation'] == 'Reference')
            new = next(row for row in data['summary'] if row['phase'] == phase and row['codec'] == codec and row['implementation'] == 'Candidate')
            if phase == 'startup':
                a, b = old['first_pcm_seconds_median']*1000, new['first_pcm_seconds_median']*1000
                target.append([codec, f'{a:.2f}', f'{b:.2f}', f'{(1-b/a)*100:.1f}% sooner', f"{old['peak_working_set_mib_median']:.2f} → {new['peak_working_set_mib_median']:.2f}"])
            else:
                a, b = old['end_to_end_seconds_median'], new['end_to_end_seconds_median']
                ca, cb = old['cpu_seconds_median'], new['cpu_seconds_median']
                target.append([codec, f'{a:.3f} → {b:.3f}', f'{(1-b/a)*100:.1f}% sooner', f'{ca:.3f} → {cb:.3f}', f'{(cb/ca-1)*100:+.1f}%', f"{old['peak_working_set_mib_median']:.2f} → {new['peak_working_set_mib_median']:.2f}"])
    text = f'''# MusicApp audio-only FFmpeg: final delivery

Compiled and deployed FFmpeg 7.1.5 with Clang 20.1.7 for the Rust application. This is an application-specific native build, not a rewrite of FFmpeg's codec algorithms. The original Python app and its installations remain unchanged.

## What improved

- FFmpeg executable: **{old_size/2**20:.2f} MiB → {new_size/2**20:.2f} MiB ({(1-new_size/old_size)*100:.2f}% smaller)**.
- Whole Rust distribution folder: **{verified['package_bytes_before']/2**20:.2f} MiB → {verified['package_bytes_after']/2**20:.2f} MiB**, including native runtime/resources and imported state on this machine. This is folder size, not a single self-contained EXE.
- Median launch-to-first-PCM improved across all six short-file cases. Most compressed formats save roughly 5 ms; WAV saves roughly 20 ms. First PCM is not first audible output or whole-app readiness.
- Long offline decoding finished 2.7–8.3% sooner across the five tested formats, and its decoder process used about 5–6 MiB less peak working-set memory. These are component figures, not a measured reduction of the whole app's RAM.

## Startup: seven trials per executable and format

{table(['Format','Full FFmpeg ms','Audio build ms','Change','Peak decoder RAM MiB'], startup_rows)}

![Startup and memory](startup.png)

Gold/reference means the original FFmpeg 7.1 essentials GCC build; teal/candidate means the deployed audio-only build.

## Sustained decoding: five trials per executable and format, 30-minute inputs

{table(['Format','Elapsed seconds: full → slim','Elapsed change','CPU seconds: full → slim','CPU change','Peak decoder RAM MiB: full → slim'], long_rows)}

![Long decoding, CPU and memory](sustained.png)

**CPU did not improve uniformly.** WAV, MP3, FLAC and AAC median CPU time increased by about 1–5%; Opus was approximately unchanged/slightly lower. The MP3 difference is about 0.25 CPU seconds per 30 minutes of decoded audio in this unpaced workload. That is an offline result, not a measured real-time playback percentage. Ranges overlap; five repeats do not establish a tiny difference as statistically certain.

CPU time sums the process's user and kernel work across all its threads. Elapsed time is the wall clock until decoding and pipe draining finish. A process can finish sooner while using slightly more total CPU. The compiler, patch release, Windows threading backend, TLS library and build configuration all differ from the reference; these experiments do not isolate which change caused each difference. Removing unused video/GPU code primarily reduces disk/loading footprint. It does not remove the computations needed to decode the same music.

The initial 120-second fixtures decoded in fractions of a second; Windows CPU accounting advanced in roughly 15.625 ms steps, exaggerating small percentage swings. The extra 30-minute-fixture run gives CPU work several seconds to accumulate. Its results are the primary sustained CPU evidence; the earlier short run remains archived. The one-repeat startup measurements in the long run are not substituted for the seven-repeat startup chart.

## What remains supported

The build retains broad native audio decoding, the app's MP3/WAV/Ogg/FLAC/M4A/Opus/WebM paths, audio extraction from MP4/WebM video containers, MP3 downloads through static LAME, ID3v2.3 Unicode tags, resampling to stereo 44.1 kHz float PCM, SponsorBlock aselect/asetpts trimming, HTTP ranges and HTTPS via Windows Schannel. Static zlib covers compressed headers/HTTP handling. FFmpeg's runtime x86 SIMD detection and speed optimization remain enabled.

Video decoders, GPU acceleration, FFplay, FFprobe, capture devices, unrelated encoders/muxers and other general-purpose FFmpeg functionality are omitted. Small internal CLI helpers may remain where FFmpeg selects them. This is not a general replacement for a full FFmpeg installation. The executable imports only Windows system DLLs; no new Python, Node, compiler, MSYS2, LAME or zlib runtime installation is required.

## Verification

- **153 compatibility checks passed, zero failed**, covering 21 generated fixture families: common integer/float WAV variants, MP3 CBR/VBR, FLAC, AAC, ALAC, Vorbis and Opus, full decode and two seeks, duration/metadata, MP3 conversion, SponsorBlock trimming, Unicode tags, MP4/WebM audio extraction, local HTTP and HTTPS byte-range seeking. PCM comparisons use explicit tolerances recorded in compatibility.json; lossy output need not be byte-identical.
- **134 short/component trials + 62 longer trials completed with zero errors.** Trial order alternates, inputs are hashed, both executables get warmups, stdout is drained in bounded blocks, and failed or incomplete output is rejected.
- **Five actual packaged Rust audio tests passed:** MP3 bypass, MP3 EQ, MP3 echo, AAC bypass and Opus bypass. All used the real audio engine/output device at volume zero, isolated test settings, advanced playback and recorded zero underflows/errors. Test media was 20 seconds long; each app run lasted four seconds.
- **50 original source hashes and 12 original profile hashes match.** Rust executable and UI hashes match their pre-change values. No Python runtime files were found in the package. Personal settings were not reset or re-imported.
- Candidate, active source tool and packaged tool have identical SHA-256: `{expected_hash}`.

Local HTTPS uses a generated test certificate with verification disabled only for those test commands; the Windows trust store is unchanged. These tests do not establish authenticated YouTube behavior, DRM/protected streams, every internet radio station, all codec variants, every Windows version, game input or fullscreen/topmost behavior. The previous parity audit's acceptance limits still apply. The new numbers must not be presented as a fresh full Python-versus-Rust application benchmark.

## Rebuild and preservation

Run `Build-FFmpeg.ps1` to rebuild the native Clang candidate. It uses existing Visual Studio/LLVM/MSYS2 plus project-local Make/NASM and source archives; it does not upgrade installed tools. `build.bat` still builds/packages the Rust app using `tools/ffmpeg.exe`. Keep the adjacent native runtime/resource folders beside MusicApp.exe.

The exact source archives, checksums, decoder allowlist and build scripts are retained under `native/ffmpeg`. The native FFmpeg build identifies itself as LGPL 2.1 or later; FFmpeg/LAME/zlib notices are supplied in `licenses`. The Rust application's aggregate GPLv3 metadata remains unchanged.

The original full FFmpeg is preserved at `native/ffmpeg/reference/ffmpeg-7.1-full.exe`; it was not removed from the Python app. The historical first Rust baseline now has a private byte-identical original FFmpeg instead of sharing the updated active tools folder. Earlier reports remain preserved. The Microsoft-compiler comparison stays at `native/ffmpeg/prefix/bin/ffmpeg.exe`; the first Clang comparison binary is retained at `native/ffmpeg/reference/ffmpeg-slim-clang-first.exe`. Earlier reports' candidate paths may now point at a later build; their recorded hashes and these retained binaries identify the actual measured releases.

## My opinion

This is a worthwhile improvement to the Rust port: the package is dramatically smaller, the decoder starts sooner, and memory savings are consistent without losing the tested audio paths. I would keep it as the default for this build. I would not describe the full port as universally faster or exhaustively one-to-one yet. For further CPU gains, profiling real playback, buffering and wakeups is more promising than rewriting mature codec algorithms blindly. The remaining account, radio and game acceptance tests still matter more than shaving another few milliseconds from a decoder that is already much faster than real time.
'''
    (DEST / 'REPORT.md').write_text(text, encoding='utf-8')
    shutil.copy2(RESULTS / 'ffmpeg-audio-final-performance/ffmpeg_startup.png', DEST / 'startup.png')
    shutil.copy2(RESULTS / 'ffmpeg-audio-long-performance/ffmpeg_sustained.png', DEST / 'sustained.png')
    for name in ['ffmpeg-audio-final-compat', 'ffmpeg-audio-final-performance', 'ffmpeg-audio-final-smoke', 'ffmpeg-audio-long-performance']:
        target = DEST / name
        target.mkdir(exist_ok=True)
        for source in (RESULTS / name).iterdir():
            if source.is_file() and source.suffix.lower() in ('.json', '.csv', '.md', '.txt', '.png'):
                shutil.copy2(source, target / source.name)
    details = DEST / 'native-build'
    details.mkdir(exist_ok=True)
    for relative in ['native/ffmpeg/sources.json', 'native/ffmpeg/audio-decoders.txt', 'native/ffmpeg/README.md', 'native/ffmpeg/build-slim.cmd', 'native/ffmpeg/build-slim.sh', 'Build-FFmpeg.ps1', 'native/ffmpeg/logs/configure-clang.log', 'native/ffmpeg/logs/dependencies-final.txt', 'licenses/FFmpeg-build.txt', 'licenses/FFmpeg-LGPL-2.1.txt', 'licenses/LAME-LGPL.txt', 'licenses/zlib-LICENSE.txt']:
        shutil.copy2(BASE / relative, details / Path(relative).name)
    # Self-contained report page; detailed tables/evidence remain in REPORT.md.
    body = '<style>body{background:#141820;color:#e6edf7;font:17px/1.6 system-ui;max-width:1150px;margin:40px auto;padding:0 20px}img{max-width:100%}a{color:#00d8b3}pre{white-space:pre-wrap;font:15px/1.6 system-ui}h1{line-height:1.2}</style>'
    body += '<h1>MusicApp audio-only FFmpeg</h1><p>Gold: original full FFmpeg. Teal: deployed audio build.</p><img src="startup.png" alt="Startup and memory"><img src="sustained.png" alt="Long decoding CPU elapsed and memory">'
    body += '<h2>Full report</h2><p><a href="REPORT.md">Open Markdown report</a></p><pre>' + html.escape(text) + '</pre>'
    (DEST / 'report.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>MusicApp FFmpeg comparison</title>' + body + '</html>', encoding='utf-8')
    archive = RESULTS / 'MusicApp-audio-FFmpeg-reports.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=7) as package:
        for source in DEST.rglob('*'):
            if source.is_file(): package.write(source, source.relative_to(DEST))
    print(DEST / 'REPORT.md')
    print(archive)


if __name__ == '__main__':
    main()
