"""Cue sheet: opening = «إذا كنت تاجر في سلة… تعال!» take A (comeA), body = take bodyA («نحلة ترد/ترسل/تبيع…»).
Times measured from pause analysis of each take; body placed 5.85 s into the composite."""
import json, subprocess
SPD = 1.06; V0 = 0.35; BOFF = 5.85
A = lambda t: round(V0 + t / SPD, 3)        # opening-take time
B = lambda t: A(t + BOFF)                   # body-take time
EOFF = 45.50
E = lambda t: A(t + EOFF)                   # closing-take time (end2)
cue = {
  'pill': A(0.06) - .05, 'come': A(1.77) - .05, 's1h': A(2.61) - .1, 'n0': A(2.80),
  's2': B(0.04) - .2, 's2l2': B(2.42) - .1,
  's3': B(4.67) - .1, 'brand': B(5.45), 'ai': B(6.35) - .05, 'bs1': B(7.61) - .1, 'bs2': B(9.50) - .1,
  's4': B(11.17) - .35, 'm1': B(11.27), 'm2': B(13.37), 'm3': B(14.75), 'm4': B(15.65), 'close': B(17.57) - .05,
  'sF': B(19.53) - .25, 'f1': B(19.53), 'f2': B(21.61) - .05, 'f3': B(23.65) - .05,
  's5': B(25.11) - .25, 's5a': B(25.97) - .1, 'stamp': B(29.23), 'k1': B(30.05) - .05, 'k2': B(31.21) - .05, 'k3': B(32.0) - .05,
  's6': B(33.34) - .25, 'c1': B(36.34) - .05, 'c2': B(37.16) - .05, 'c3': B(37.85) - .05, 'c4': B(38.5) - .05,
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
print(json.dumps({k: cue[k] for k in ('s7', 's8', 'up', 'guar', 'upb', 'risk', 'btn', 'salla', 'end')}))
