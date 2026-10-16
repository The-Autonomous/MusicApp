"""Paired comparison gate; leaves candidate deployment to the parent task."""
import argparse
import json
import math
from pathlib import Path
import statistics


def comparison(old, new):
    deltas=[b-a for a,b in zip(old,new)]
    n=len(deltas)
    critical={5:2.776445,7:2.446912,9:2.306004,15:2.144787}.get(n,3.0)
    mean=statistics.mean(deltas)
    margin=critical*statistics.stdev(deltas)/math.sqrt(n) if n>1 else float('inf')
    return {'paired_trials':n,'reference_mean':statistics.mean(old),'candidate_mean':statistics.mean(new),
            'mean_difference':mean,'mean_change_percent':(statistics.mean(new)/statistics.mean(old)-1)*100,
            'paired_t95_interval':[mean-margin,mean+margin]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    args=parser.parse_args()
    data=json.loads(args.report.read_text(encoding='utf-8-sig'))
    if data['errors']: raise RuntimeError('Cannot assess a failed benchmark')
    runs=[r for r in data['runs'] if r['phase']=='sustained']
    codecs=list(dict.fromkeys(r['codec'] for r in runs))
    repeats=sorted(set(r['repeat'] for r in runs))
    details={}
    totals={metric:{impl:[0.0]*len(repeats) for impl in ('Reference','Candidate')} for metric in ('cpu_seconds','end_to_end_seconds')}
    for codec in codecs:
        details[codec]={}
        for metric in totals:
            values={impl:[next(r[metric] for r in runs if r['codec']==codec and r['repeat']==repeat and r['implementation']==impl) for repeat in repeats] for impl in ('Reference','Candidate')}
            details[codec][metric]=comparison(values['Reference'],values['Candidate'])
            for impl in values:
                totals[metric][impl]=[a+b for a,b in zip(totals[metric][impl],values[impl])]
    aggregate={metric:comparison(values['Reference'],values['Candidate']) for metric,values in totals.items()}
    cpu=aggregate['cpu_seconds']
    # Predefined before measurements: at least 2% total CPU reduction, paired
    # t interval below zero, no codec mean regression above 5%, MP3 above 2%.
    accepted=(cpu['mean_change_percent']<=-2 and cpu['paired_t95_interval'][1]<0
              and all(v['cpu_seconds']['mean_change_percent']<=5 for v in details.values())
              and details['MP3']['cpu_seconds']['mean_change_percent']<=2)
    report={'accepted_cpu_improvement':accepted,'aggregate':aggregate,'by_codec':details,
            'limits':'Approximate paired t intervals on this machine/workload; multiple codec comparisons are descriptive. No claim of universal speedups or audible latency.'}
    output=args.report.parent/'cpu-assessment.json'
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
