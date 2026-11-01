"""Synthesize an original music bed + SFX for the Nahla Salla-merchant spot.
Everything is generated from scratch (no samples) -> no licensing concerns.
Timeline must match src/index.html  (T = s3 7.5, s4 11.6, s5 20.6, s6 26.0, s7 30.0, end 34.5)
"""
import numpy as np, wave

SR = 48000
import json
K = json.load(open('/tmp/nahla-video/cues_v3.json'))
DUR = K['end']
N = int(SR * DUR)
rng = np.random.default_rng(7)
BPM = 112
BEAT = 60 / BPM            # 0.5357 s ; 14 beats == 7.5 s (drop lands on the reveal)
BAR = BEAT * 4

music = np.zeros((N, 2))
sfx = np.zeros((N, 2))


def t_arr(d):
    return np.arange(int(SR * d)) / SR


def place(buf, start, sig, gain=1.0, pan=0.0):
    i = int(start * SR)
    if i >= N or i + len(sig) <= 0:
        return
    if sig.ndim == 1:
        l = np.sqrt((1 - pan) / 2); r = np.sqrt((1 + pan) / 2)
        sig = np.stack([sig * l * 1.414, sig * r * 1.414], 1)
    j = min(N, i + len(sig))
    off = max(0, -i)
    buf[max(i, 0):j] += sig[off:j - i] * gain


def onepole_lp(x, fc):
    """fc may be scalar or array (time-varying)."""
    fc = np.broadcast_to(np.asarray(fc, float), x.shape[:1])
    a = np.exp(-2 * np.pi * fc / SR)
    y = np.zeros_like(x); s = 0.0
    for n in range(len(x)):
        s = (1 - a[n]) * x[n] + a[n] * s
        y[n] = s
    return y


def lp_fast(x, fc):
    # fixed-cutoff one-pole using scipy-free vectorised recursion via lfilter emulation
    a = np.exp(-2 * np.pi * fc / SR)
    y = np.empty_like(x); s = 0.0
    for n in range(len(x)):
        s = (1 - a) * x[n] + a * s
        y[n] = s
    return y


def hp(x, fc):
    return x - lp_fast(x, fc)


def note(n):  # midi -> hz
    return 440 * 2 ** ((n - 69) / 12)


def env_ad(d, a=0.005, decay=0.3):
    t = t_arr(d)
    e = np.minimum(1, t / max(a, 1e-4)) * np.exp(-np.maximum(0, t - a) / decay)
    return e


# ------------------------------------------------------------------ instruments
def kick():
    t = t_arr(0.45)
    f = 48 + 110 * np.exp(-t * 28)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * np.exp(-t * 7.5) + 0.25 * np.sin(ph * 2) * np.exp(-t * 30)


def clap():
    d = 0.22; t = t_arr(d)
    n = rng.standard_normal(len(t))
    n = hp(n, 900)
    e = np.zeros_like(t)
    for k, o in enumerate([0, 0.011, 0.022]):
        e += (t >= o) * np.exp(-np.maximum(0, t - o) * (60 if k < 2 else 16))
    return n * e * 0.5


def hat(open_=False):
    d = 0.18 if open_ else 0.05; t = t_arr(d)
    n = hp(rng.standard_normal(len(t)), 6000)
    return n * np.exp(-t * (18 if open_ else 70)) * 0.35


def pluck(freq, d=0.35):
    t = t_arr(d)
    s = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(4 * np.pi * freq * t) + 0.12 * np.sin(6 * np.pi * freq * t)
    return s * env_ad(d, 0.003, 0.12)


def bell(freq, d=0.9, dec=0.35):
    t = t_arr(d)
    s = (np.sin(2 * np.pi * freq * t) + 0.5 * np.sin(2 * np.pi * freq * 2.01 * t) * np.exp(-t * 6)
         + 0.25 * np.sin(2 * np.pi * freq * 3.0 * t) * np.exp(-t * 10))
    return s * env_ad(d, 0.002, dec)


def pad_chord(notes, d, bright=2400):
    t = t_arr(d)
    s = np.zeros(len(t))
    for n in notes:
        f = note(n)
        for det in (-0.12, 0.0, 0.12):
            ff = f * 2 ** (det / 12)
            s += (2 * ((ff * t + rng.random()) % 1) - 1) * 0.22  # saw
    s = lp_fast(s, bright)
    a = np.minimum(1, t / 0.25) * np.minimum(1, (d - t) / 0.3)
    return s * a * 0.22


def bass(freq, d):
    t = t_arr(d)
    s = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(4 * np.pi * freq * t)
    return s * np.minimum(1, t / 0.01) * np.exp(-t * 2.2) * np.minimum(1, (d - t) / 0.03)



# ------------------------------------------------------------------ music (v2: VO-driven timeline)
PROG = [([53, 57, 60, 65], 41), ([55, 59, 62, 67], 43), ([57, 60, 64, 69], 45), ([60, 64, 67, 72], 48)]
DROP = K['s3'] + 0.05
# intro grid counted backwards from the drop so the downbeat lands on the reveal
nint = int(DROP / BEAT)
g0 = DROP - nint * BEAT
for b in range(nint):
    t0 = g0 + b * BEAT
    place(music, t0, hat(), 0.5 if b % 2 else 0.8, pan=0.3)
    place(music, t0 + BEAT / 2, hat(), 0.35, pan=-0.3)
place(music, 0.0, pad_chord([57, 60, 64], K['s2'] + 0.3, 1400), 0.9)
place(music, K['s2'], pad_chord([53, 57, 60], DROP - K['s2'], 700), 0.9)
for k, n in enumerate([69, 72, 76, 72, 69, 72, 76, 79]):
    place(music, g0 + k * BEAT, pluck(note(n)), 0.2, pan=(-0.4 if k % 2 else 0.4))
pb = int((DROP - K['s2']) / BEAT)
for b in range(pb):
    place(music, DROP - (pb - b) * BEAT, bass(note(33), BEAT * 0.9), 0.35)
# riser into the drop
d = 1.45; t = t_arr(d)
nz = rng.standard_normal(len(t))
riser = onepole_lp(nz, 300 + 9000 * (t / d) ** 2) * (t / d) ** 1.6 * 0.9
ph = 2 * np.pi * np.cumsum(220 * 2 ** (2 * t / d)) / SR
riser += np.sin(ph) * (t / d) ** 2 * 0.12
place(sfx, DROP - d, riser, 0.5)

END_MUSIC = K['end'] - 0.2
BREAK = (K['s7'] - BAR * 0.5, K['s7'])
nbeats = int((END_MUSIC - DROP) / BEAT)
for b in range(nbeats):
    t0 = DROP + b * BEAT
    chord, root = PROG[(b // 4) % 4]
    in_break = BREAK[0] <= t0 < BREAK[1]
    outro = t0 >= K['vo_end'] + 0.2
    if not in_break and not outro:
        place(music, t0, kick(), 0.95)
    if b % 4 in (1, 3) and not in_break and not outro:
        place(music, t0, clap(), 0.5)
    if not outro:
        place(music, t0 + BEAT / 2, hat(open_=(b % 2 == 1)), 0.5, pan=0.25)
        place(music, t0, hat(), 0.22, pan=-0.25)
    if b % 4 == 0:
        place(music, t0, pad_chord(chord, BAR + 0.1, 2600), 1.0)
    if b % 4 in (0, 2) and not outro:
        place(music, t0, bass(note(root), BEAT * 0.95), 0.55)
    if b % 4 in (1, 3) and not outro:
        place(music, t0 + BEAT / 2, bass(note(root + 12), BEAT * 0.45), 0.35)
    arp = [chord[0] + 12, chord[1] + 12, chord[2] + 12, chord[1] + 12]
    for h in range(2):
        place(music, t0 + h * BEAT / 2, pluck(note(arp[(b * 2 + h) % 4]), 0.3), 0.16, pan=(0.35 if h else -0.35))
place(music, K['s7'], pad_chord([60, 64, 67, 72, 76], K['end'] - K['s7'], 3200), 0.9)
# ------------------------------------------------------------------ SFX
def whoosh(d=0.55, up=True):
    t = t_arr(d)
    n = rng.standard_normal(len(t))
    fc = 400 + 5000 * (t / d if up else 1 - t / d)
    s = onepole_lp(n, fc)
    s = s - onepole_lp(s, 250)
    e = np.sin(np.pi * t / d) ** 2
    return s * e * 1.6


def pop(f0=900, f1=1500, d=0.09):
    t = t_arr(d)
    f = f0 + (f1 - f0) * (t / d)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 38)


def notif(base):
    return np.concatenate([bell(base, 0.12, 0.06) * 0.8, bell(base * 1.5, 0.45, 0.14)])


def recv():
    return np.concatenate([bell(note(81), 0.09, 0.05) * 0.6, bell(note(88), 0.35, 0.12)])


def send():
    return pop(700, 1600, 0.08) * 0.9


def chime_cash():
    out = np.zeros(int(SR * 1.6))
    for k, n in enumerate([84, 88, 91, 96]):
        s = bell(note(n), 1.2, 0.4)
        i = int(k * 0.07 * SR)
        out[i:i + len(s)] += s * 0.6
    sp = hp(rng.standard_normal(len(out)), 7000) * np.exp(-t_arr(1.6) * 5) * 0.15
    return out + sp


def impact(big=True):
    d = 1.6 if big else 0.8; t = t_arr(d)
    f = 42 + 90 * np.exp(-t * 14)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * (2.6 if big else 5))
    crash = hp(rng.standard_normal(len(t)), 3000) * np.exp(-t * (3 if big else 7)) * 0.3
    return boom + crash


def thud():
    t = t_arr(0.35)
    f = 60 + 120 * np.exp(-t * 30)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 12) + hp(rng.standard_normal(len(t)), 1500) * np.exp(-t * 45) * 0.4


def scratch(d=0.32):
    t = t_arr(d)
    n = rng.standard_normal(len(t))
    s = onepole_lp(n, 1500 + 3000 * t / d)
    return s * np.sin(np.pi * t / d) * 1.2


def tick():
    t = t_arr(0.06)
    return np.sin(2 * np.pi * 2200 * t) * np.exp(-t * 90) * 0.7


def buzz(d=0.9, f=190):
    t = t_arr(d)
    vib = f * (1 + 0.035 * np.sin(2 * np.pi * 23 * t)) * (1 + 0.15 * np.sin(np.pi * t / d))
    ph = 2 * np.pi * np.cumsum(vib) / SR
    s = (2 * ((ph / (2 * np.pi)) % 1) - 1)
    s = lp_fast(s, 1800)
    am = 0.75 + 0.25 * np.sin(2 * np.pi * 95 * t)
    return s * am * np.sin(np.pi * t / d) ** 1.5 * 0.5


def sparkle(d=0.9):
    out = np.zeros(int(SR * d))
    for k in range(7):
        s = bell(note(91 + (k * 5) % 12), 0.4, 0.12) * 0.25
        i = int(k * 0.08 * SR)
        out[i:i + len(s)] += s[:len(out) - i]
    return out



# ------------------------------------------------------------------ SFX on VO cues (kept light so the voice leads)
place(sfx, max(0, K['pill'] - .05), whoosh(0.45), 0.3)
nstep = (K['s2'] - .5 - K['n0']) / 5
for i in range(5):
    place(sfx, K['n0'] + i * nstep, notif(note(76 + i * 2)), 0.22, pan=(-0.3 + i * 0.15))
place(sfx, K['s2'], impact(False), 0.3)
for i in range(5):
    place(sfx, K['s2l2'] + .1 + i * .22, whoosh(0.4, up=False), 0.12, pan=-0.6)
place(sfx, DROP - 0.08, impact(True), 0.75)
place(sfx, K['brand'], sparkle(), 0.35)
place(sfx, K['s4'] - .3, whoosh(0.5), 0.3)
place(sfx, K['m1'], recv(), 0.3, pan=0.25)
place(sfx, K['m2'], send(), 0.35, pan=-0.25)
place(sfx, K['m3'], recv(), 0.3, pan=0.25)
place(sfx, K['m4'], send(), 0.35, pan=-0.25)
place(sfx, K['close'] + .05, chime_cash(), 0.45)
place(sfx, K['sF'] - .3, whoosh(0.5), 0.3)
for c in ('f1', 'f2', 'f3'):
    place(sfx, K[c], pop(500, 1100, 0.1), 0.3)
place(sfx, K['s5'] - .3, whoosh(0.5), 0.3)
place(sfx, K['stamp'] - .45, scratch(), 0.25)
place(sfx, K['stamp'] + .25, thud(), 0.75)
for c in ('k1', 'k2', 'k3'):
    place(sfx, K[c], tick(), 0.4)
place(sfx, K['s6'] - .3, whoosh(0.5), 0.3)
for c in ('c1', 'c2', 'c3', 'c4'):
    place(sfx, K[c], tick(), 0.35)
place(sfx, K['s7'] - .3, whoosh(0.5), 0.35)
place(sfx, K['s7'], impact(True), 0.45)
place(sfx, K['btn'], pop(500, 1300, 0.12), 0.35)
place(sfx, K['btn'] + .8, sparkle(), 0.3)
# S8: sales growth + guarantee
place(sfx, K['s8'] - .3, whoosh(0.5), 0.35)
place(sfx, K['s8'] + 2.0, sparkle(), 0.35)
place(sfx, K['guar'], pop(500, 1100, 0.12), 0.35)
place(sfx, K['risk'] + .05, thud(), 0.6)

# ------------------------------------------------------------------ voice + ducking + mix
import wave as _w
vw = _w.open('/tmp/nahla-video/vo_v3.wav'); vo = np.frombuffer(vw.readframes(vw.getnframes()), np.int16).reshape(-1, 2) / 32768.0
voice = np.zeros((N, 2)); place(voice, K['vo_start'], vo, 1.0)
# duck music by VO envelope (smoothed RMS -> gain)
env = np.sqrt(np.convolve(voice[:, 0] ** 2, np.ones(2400) / 2400, mode='same'))
active = (env > 0.01).astype(float)
k = int(0.25 * SR); active = np.convolve(active, np.ones(k) / k, mode='same')
duck = 1.0 - 0.45 * np.clip(active, 0, 1)          # ~ -5 dB under speech

def norm_peak(x, peak):
    m = np.max(np.abs(x)) + 1e-9
    return x * (peak / m)

def rms(x): return np.sqrt(np.mean(x ** 2) + 1e-12)
voice = voice * (0.11 / rms(voice[voice[:, 0] != 0]))  # speech RMS target
music = norm_peak(music, 0.22) * duck[:, None]
sfx = norm_peak(sfx, 0.30)
mix = voice + music + sfx
t = np.arange(N) / SR
mix *= np.minimum(1, t / 0.08)[:, None]
mix *= np.clip((DUR - t) / 1.0, 0, 1)[:, None]
print('pre-peak', np.max(np.abs(mix)))
sp = voice[:,0] != 0
print('voice rms dB', 20*np.log10(rms(voice[sp])), 'music-under-voice dB', 20*np.log10(rms(music[sp])), 'music-solo dB', 20*np.log10(rms(music[~sp & (t<K['s3'])])))
mix = np.tanh(mix * 1.0) / np.tanh(1.0) if np.max(np.abs(mix)) > 0.98 else mix
pcm = (mix * 32767).astype(np.int16)
with wave.open('/tmp/nahla-video/audio_v3.wav', 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
print('ok', DUR)
