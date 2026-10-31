"""Cue sheet measured on VO take A (ElevenLabs, Abdulrahman voice), in ORIGINAL take seconds.
Converted to absolute video time after a pitch-preserving speed-up."""
import json, subprocess
SPD = 1.06      # rubberband tempo (pitch preserved)
V0 = 0.35       # VO starts this many seconds into the video
OFF = 0.15   # opening take is longer: later takeA cues shift by this much
A = lambda t: round(V0 + t / SPD, 3)
B = lambda t: A(t + OFF)
cue = {
  # S1  "يا تاجر سلة… | عملاءك يسألونك على الواتساب، ليل ونهار."
  'pill': A(0.09) - .05, 's1h': A(1.33) - .1, 'n0': A(1.60),
  # S2  "وكل رسالة تتأخر عليها… | بيعة تروح لمنافسك."
  's2': B(4.89) - .2, 's2l2': B(7.07) - .1,
  # S3  "عشان كذا جات نحلة! | ذكاء اصطناعي يرد ويبيع… | على واتساب أعمالك."
  's3': B(8.98) - .1, 'brand': B(9.70), 'bs1': B(10.69) - .1, 'bs2': B(12.68) - .1,
  # S4  "ترد على عميلك بثواني، | تعرف منتجاتك وأسعارك، | وترسل له رابط الطلب… | وتقفل البيعة."
  's4': B(14.25) - .35, 'm1': B(14.35), 'm2': B(15.97), 'm3': B(16.95), 'm4': B(17.79), 'close': B(19.31) - .05,
  # SF  "وتتابع السلات المتروكة، | وترسل حملاتك وعروضك… | على مدار الساعة."
  'sF': B(20.57) - .25, 'f1': B(20.57), 'f2': B(22.38) - .05, 'f3': B(24.06) - .05,
  # S5  "والأهم؟ | واتساب أعمالك يبقى في جوالك… | بلا حذف! | نفس رقمك، | ونفس تطبيقك، وترد بنفسك متى ما بغيت."
  's5': B(25.36) - .25, 's5a': B(26.27) - .1, 'stamp': B(29.45), 'k1': B(30.32) - .05, 'k2': B(31.47) - .05, 'k3': B(32.35) - .05,
  # S6  "ومربوطة مباشرة بمتجرك في سلة… | منتجاتك، | أسعارك، | مخزونك، | وطلباتك."
  's6': B(33.62) - .25, 'c1': B(36.03) - .05, 'c2': B(36.97) - .05, 'c3': B(37.65) - .05, 'c4': B(38.40) - .05,
  # S7  "نحلة… | واتسابك يبقى معك، | ونحلة تبيع معك. | جرّبها مجاناً أربعطعش يوم، | من متجر تطبيقات سلة."
  's7': B(39.51) - .25, 't1': B(40.35) - .05, 't2': B(41.83) - .05, 'btn': B(43.23) - .1, 'salla': B(45.25) - .05,
  'vo_end': B(46.69),
}
cue['end'] = round(cue['vo_end'] + 1.8, 2)
cue = {k: round(v, 3) for k, v in cue.items()}
open('/tmp/nahla-video/src/timing_v3.js', 'w').write('window.K = ' + json.dumps(cue, indent=1) + ';\n')
json.dump({'SPD': SPD, 'V0': V0, **cue}, open('/tmp/nahla-video/cues_v3.json', 'w'), indent=1)
# VO: speed up (pitch preserved), 48k stereo, gentle polish (HPF, light compression)
subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', '/tmp/nahla-video/vo_dl/composite.wav',
  '-af', f'rubberband=tempo={SPD}:pitch=1:formant=preserved,highpass=f=80,acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120,aresample=48000',
  '-ac', '2', '/tmp/nahla-video/vo_v3.wav'], check=True)
print(json.dumps(cue))
