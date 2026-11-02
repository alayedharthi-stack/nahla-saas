"""Cue sheet for VO v8 (see build_vo_v8.py). Times are in each source take; mapped to composite, then to video time."""
import json, subprocess
SPD = 1.06; V0 = 0.35
seg = {k: tuple(v) for k, v in json.load(open('/tmp/nahla-video/vo_dl/segments_v8.json')).items()}
def comp(i, t):
    k, a, start = seg[str(i)]; return start + (t - a)
V = lambda c: round(V0 + c / SPD, 3)
O = lambda t: V(comp(0, t))   # opening (comeA)
A1 = lambda t: V(comp(1, t))  # take A: S2–S3
Bd = lambda t: V(comp(2, t))  # bodyA: «نحلة ترد على عميلك بثواني»
A2 = lambda t: V(comp(3, t))  # take A: rest of S4 … S6
E = lambda t: V(comp(4, t))   # closing (end2)
cue = {
  'pill': O(0.06) - .05, 'come': O(1.77) - .05, 's1h': O(2.61) - .1, 'n0': O(2.80),
  's2': A1(4.89) - .2, 's2l2': A1(7.07) - .1,
  's3': A1(8.98) - .1, 'brand': A1(9.70), 'ai': A1(10.69) - .1, 'bs1': A1(10.69) - .1, 'bs2': A1(12.68) - .1,
  's4': Bd(11.17) - .35, 'm1': Bd(11.27), 'm2': A2(15.97), 'm3': A2(16.95), 'm4': A2(17.79), 'close': A2(19.31) - .05,
  'sF': A2(20.57) - .25, 'f1': A2(20.57), 'f2': A2(22.38) - .05, 'f3': A2(24.06) - .05,
  's5': A2(25.36) - .25, 's5a': A2(26.27) - .1, 'stamp': A2(29.45), 'k1': A2(30.32) - .05, 'k2': A2(31.47) - .05, 'k3': A2(32.35) - .05,
  's6': A2(33.62) - .25, 'c1': A2(36.03) - .05, 'c2': A2(36.97) - .05, 'c3': A2(37.65) - .05, 'c4': A2(38.40) - .05,
  's7': E(0.03) - .25, 't1': E(0.97) - .05, 't2': E(2.54) - .05,
  's8': E(4.27) - .15, 'up': E(5.18) - .05, 'guar': E(7.15) - .1, 'upb': E(9.24) - .05, 'risk': E(10.62) - .05,
  'btn': E(12.34) - .1, 'salla': E(13.30) - .05,
  'vo_end': E(14.49),
}
cue['end'] = round(cue['vo_end'] + 1.8, 2)
cue = {k: round(v, 3) for k, v in cue.items()}
open('/tmp/nahla-video/src/timing_v3.js', 'w').write('window.K = ' + json.dumps(cue, indent=1) + ';\n')
json.dump({'SPD': SPD, 'V0': V0, 'vo_start': V0, **cue}, open('/tmp/nahla-video/cues_v3.json', 'w'), indent=1)
subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', '/tmp/nahla-video/vo_dl/composite.wav',
  '-af', f'rubberband=tempo={SPD}:pitch=1:formant=preserved,highpass=f=80,acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120,aresample=48000',
  '-ac', '2', '/tmp/nahla-video/vo_v3.wav'], check=True)
print(json.dumps({k: cue[k] for k in ('s2', 's3', 's4', 'sF', 's5', 's6', 's7', 's8', 'end')}))
