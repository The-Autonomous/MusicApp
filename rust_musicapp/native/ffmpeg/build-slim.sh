#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
task_root="$PWD"
export TMPDIR="$task_root/temp"
variant="${FFMPEG_VARIANT:-msvc}"
suffix=""
compiler_flags=()
if [[ "$variant" == clang* ]]; then
  suffix="-$variant"
  compiler_flags=(--cc=clang-cl --host-cc=clang-cl '--optflags=-O2 -clang:-O3')
fi
if [ "$variant" = clang-pgo-train ]; then
  mkdir -p "$task_root/profiles/configure"
  export LLVM_PROFILE_FILE="$(cygpath -m "$task_root/profiles/configure")/%m-%p.profraw"
  compiler_flags=(--cc=clang-cl --host-cc=clang-cl '--optflags=-O2 -clang:-O3 -clang:-fno-math-errno -clang:-fprofile-generate')
fi
if [ "$variant" = clang-pgo ]; then
  test -s "$task_root/profiles/musicapp.profdata"
  compiler_flags=(--cc=clang-cl --host-cc=clang-cl "--optflags=-O2 -clang:-O3 -clang:-fno-math-errno -clang:-fprofile-use=$(cygpath -m "$task_root/profiles/musicapp.profdata") -clang:-Wno-profile-instr-unprofiled -clang:-Wno-profile-instr-out-of-date")
fi
mkdir -p "build/ffmpeg$suffix" prefix/bin
cd "build/ffmpeg$suffix"
audio_decoders="$(tr -d '\r\n' < "$task_root/audio-decoders.txt")"
"$task_root/sources/ffmpeg-7.1.5/configure" \
  --prefix="$task_root/prefix" --toolchain=msvc --arch=x86_64 --target-os=win64 \
  "${compiler_flags[@]}" \
  --disable-everything --disable-autodetect --disable-doc --disable-debug \
  --disable-ffplay --disable-ffprobe --disable-avdevice --disable-swscale --disable-postproc \
  --disable-shared --enable-static --enable-ffmpeg --enable-network --enable-w32threads \
  --enable-schannel --enable-zlib --enable-libmp3lame \
  --enable-decoder="$audio_decoders" \
  --enable-parser=aac,aac_latm,ac3,dca,flac,mpegaudio,opus,vorbis \
  --enable-demuxer=mp3,wav,ogg,flac,mov,matroska,aac,hls,mpegts \
  --enable-encoder=libmp3lame,pcm_f32le \
  --enable-muxer=mp3,pcm_f32le \
  --enable-filter=aresample,aformat,anull,atrim,aselect,asetpts \
  --enable-protocol=file,pipe,http,https,tcp,tls,httpproxy,crypto,data \
  --extra-cflags="-I$(cygpath -m "$task_root/prefix/include")" \
  --extra-ldflags="-libpath:$(cygpath -m "$task_root/prefix/lib") -OPT:REF -OPT:ICF" \
  --extra-version="MusicApp-audio$suffix" \
  2>&1 | tee "$task_root/logs/configure$suffix.log"
make -j4 ffmpeg.exe 2>&1 | tee "$task_root/logs/compile$suffix.log"
cp ffmpeg.exe "$task_root/prefix/bin/ffmpeg$suffix.exe"
"$task_root/prefix/bin/ffmpeg$suffix.exe" -hide_banner -version > "$task_root/logs/version$suffix.txt" 2>&1
