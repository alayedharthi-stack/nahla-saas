"""Cue sheet measured on VO take A (ElevenLabs, Abdulrahman voice), in ORIGINAL take seconds.
Converted to absolute video time after a pitch-preserving speed-up."""
import json, subprocess
SPD = 1.06      # rubberband tempo (pitch preserved)
V0 = 0.35       # VO starts this many seconds into the video
A = lambda t: round(V0 + t / SPD, 3)
cue = {
  # S1  "يا تاجر سلة… | عملاءك يسألونك على الواتساب، ليل ونهار."
  'pill': A(0.06) - .05, 's1h': A(1.25) - .1, 'n0': A(1.55),
  # S2  "وكل رسالة تتأخر عليها… | بيعة تروح لمنافسك."
  's2': A(4.89) - .2, 's2l2': A(7.07) - .1,
  # S3  "عشان كذا جات نحلة! | ذكاء اصطناعي يرد ويبيع… | على واتساب أعمالك."
  's3': A(8.98) - .1, 'brand': A(9.70), 'bs1': A(10.69) - .1, 'bs2': A(12.68) - .1,
  # S4  "ترد على عميلك بثواني، | تعرف منتجاتك وأسعارك، | وترسل له رابط الطلب… | وتقفل البيعة."
  's4': A(14.25) - .35, 'm1': A(14.35), 'm2': A(15.97), 'm3': A(16.95), 'm4': A(17.79), 'close': A(19.31) - .05,
  # SF  "وتتابع السلات المتروكة، | وترسل حملاتك وعروضك… | على مدار الساعة."
  'sF': A(20.57) - .25, 'f1': A(20.57), 'f2': A(22.38) - .05, 'f3': A(24.06) - .05,
  # S5  "والأهم؟ | واتساب أعمالك يبقى في جوالك… | بلا حذف! | نفس رقمك، | ونفس تطبيقك، وترد بنفسك متى ما بغيت."
  's5': A(25.36) - .25, 's5a': A(26.27) - .1, 'stamp': A(29.45), 'k1': A(30.32) - .05, 'k2': A(31.47) - .05, 'k3': A(32.35) - .05,
  # S6  "ومربوطة مباشرة بمتجرك في سلة… | منتجاتك، | أسعارك، | مخزونك، | وطلباتك."
  's6': A(33.62) - .25, 'c1': A(36.03) - .05, 'c2': A(36.97) - .05, 'c3': A(37.65) - .05, 'c4': A(38.40) - .05,
  # S7  "نحلة… | واتسابك يبقى معك، | ونحلة تبيع معك. | جرّبها مجاناً أربعطعش يوم، | من متجر تطبيقات سلة."
  's7': A(39.51) - .25, 't1': A(40.35) - .05, 't2': A(41.83) - .05, 'btn': A(43.23) - .1, 'salla': A(45.25) - .05,
  'vo_end': A(46.69),
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
