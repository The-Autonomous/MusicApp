"""Generate deterministic PGO training inputs distinct from timed fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from benchmark_ffmpeg import decoder_arguments, CREATE_NO_WINDOW

BASE = Path(__file__).resolve().parents[1]


def run(binary, arguments, drain=False):
    process = subprocess.Popen([str(binary), *map(str, arguments)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    if drain:
        while process.stdout.read(65536): pass
        error = process.stderr.read()
        process.wait(timeout=120)
    else:
        _, error = process.communicate(timeout=120)
    if process.returncode: raise RuntimeError(error.decode('utf-8', 'replace')[-2000:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generator', type=Path, required=True)
    parser.add_argument('--instrumented', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.generator, args.instrumented, args.output = (p.resolve() for p in (args.generator, args.instrumented, args.output))
    if not args.output.is_relative_to(BASE): parser.error('Training output must stay inside the Rust project')
    args.output.mkdir(parents=True, exist_ok=True)
    source = args.output/'training-source.wav'
    signal = ('aevalsrc=0.13*sin(2*PI*(220*t+70*t*t))*(0.6+0.4*sin(2*PI*0.7*t))+'
              '0.06*sin(2*PI*880*t)+0.025*sin(2*PI*13007*t)|'
              '0.12*sin(2*PI*(330*t+55*t*t))+0.06*sin(2*PI*1760*t)*(0.5+0.5*cos(2*PI*2.3*t)):s=48000:d=45')
    run(args.generator, ['-nostdin','-hide_banner','-loglevel','error','-y','-threads','1','-filter_threads','1','-f','lavfi','-i',signal,'-c:a','pcm_s24le',source])
    cases = [('pcm16','wav',['-c:a','pcm_s16le']), ('mono','wav',['-c:a','pcm_s16le','-ac','1','-ar','22050']),
             ('mp3','mp3',['-c:a','libmp3lame','-b:a','192k']), ('mp3vbr','mp3',['-c:a','libmp3lame','-q:a','2']),
             ('flac','flac',['-c:a','flac']), ('aac','m4a',['-c:a','aac','-b:a','160k']),
             ('alac','m4a',['-c:a','alac']), ('opus','webm',['-c:a','libopus','-b:a','96k']),
             ('vorbis','ogg',['-c:a','libvorbis','-q:a','4'])]
    records=[]
    for name, extension, codec in cases:
        media=args.output/f'{name}.{extension}'
        run(args.generator,['-nostdin','-hide_banner','-loglevel','error','-y','-threads','1','-filter_threads','1','-i',source,*codec,media])
        run(args.instrumented, decoder_arguments(media), drain=True)
        records.append({'fixture':media.name,'sha256':hashlib.sha256(media.read_bytes()).hexdigest(),'seconds':45})
        print('Trained',name,flush=True)
    report={'scope':'Synthetic training only; different signal from timed comparison inputs; no user media or profiles read.',
            'instrumented_sha256':hashlib.sha256(args.instrumented.read_bytes()).hexdigest(),'fixtures':records}
    (args.output/'training.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__': main()
