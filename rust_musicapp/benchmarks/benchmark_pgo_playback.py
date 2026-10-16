"""Compare real Rust playback with two helpers; app/UI binaries are identical."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
from types import SimpleNamespace
from run_benchmarks import run_one, write_results

BASE=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',type=Path,required=True)
    parser.add_argument('--after',type=Path,required=True)
    parser.add_argument('--media',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=20)
    parser.add_argument('--repeats',type=int,default=3)
    args=parser.parse_args()
    args.before,args.after,args.media,args.output=(p.resolve() for p in (args.before,args.after,args.media,args.output))
    if not args.output.is_relative_to(BASE): parser.error('Output must remain in the Rust project')
    if hashlib.sha256(args.before.read_bytes()).digest()!=hashlib.sha256(args.after.read_bytes()).digest(): parser.error('Application binaries must be identical')
    args.output.mkdir(parents=True)
    options=SimpleNamespace(previous_rust_exe=args.before,rust_exe=args.after,python=None,profile={},seconds=args.seconds,warmup=2,interval=.2,startup_timeout=30)
    data={'created_utc':datetime.now(timezone.utc).isoformat(),'scope':'Actual muted Rust playback with identical app/UI and different native FFmpeg helpers; includes observed child process CPU. Fresh isolated state; no hooks/network/account access.',
          'configuration':{'seconds':args.seconds,'repeats':args.repeats,'media_sha256':hashlib.sha256(args.media.read_bytes()).hexdigest()},'artifacts':{},'runs':[],'errors':[]}
    for name,exe in [('before',args.before),('after',args.after)]:
        data['artifacts'][name]={'app_sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),'ffmpeg_sha256':hashlib.sha256((exe.parent/'tools/ffmpeg.exe').read_bytes()).hexdigest(),
                                 'ui_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (exe.parent/'ui').glob('*') if p.is_file()}}
    if data['artifacts']['before']['ui_sha256']!=data['artifacts']['after']['ui_sha256']: parser.error('UI files must be identical')
    for repeat in range(1,args.repeats+1):
        order=('Rust previous','Rust') if repeat%2 else ('Rust','Rust previous')
        for implementation in order:
            print('Playback',implementation,repeat,flush=True)
            try: data['runs'].append(run_one(implementation,'playback_bypass',repeat,args.output,options,args.media))
            except Exception as error: data['errors'].append({'implementation':implementation,'repeat':repeat,'error':str(error)})
            write_results(data,args.output)
    lines=['# Actual Rust MP3 playback','',data['scope'],'', '| Helper | Mean CPU, one core % | Mean working set, MiB | Audio underflows |','|---|---:|---:|---:|']
    for name in ('Rust previous','Rust'):
        rows=[r for r in data['runs'] if r['implementation']==name]
        if rows: lines.append(f"| {name} | {statistics.mean(r['cpu_one_core_percent'] for r in rows):.4f} | {statistics.mean(r['rss_mean_mib'] for r in rows):.2f} | {sum(r['audio_validation']['underflows'] for r in rows)} |")
    lines+=['','Rust previous means the previous slim FFmpeg; Rust means the PGO candidate. Application executable and UI hashes match. CPU includes both Rust and observed FFmpeg children. Three short repeats cannot resolve very small CPU differences confidently. No claim of game/account or exhaustive parity.']
    (args.output/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    raise SystemExit(1 if data['errors'] else 0)


if __name__=='__main__': main()
