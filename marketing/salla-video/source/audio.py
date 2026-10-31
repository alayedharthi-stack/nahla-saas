"""Synthesize an original music bed + SFX for the Nahla Salla-merchant spot.
Everything is generated from scratch (no samples) -> no licensing concerns.
Timeline must match src/index.html  (T = s3 7.5, s4 11.6, s5 20.6, s6 26.0, s7 30.0, end 34.5)
"""
import numpy as np, wave

SR = 48000
DUR = 34.5
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


# ------------------------------------------------------------------ music
# key: A minor / C major ; progression for the drop:  F  G  Am  C  (vi-IV-V-I feel, uplifting)
PROG = [([53, 57, 60, 65], 41), ([55, 59, 62, 67], 43), ([57, 60, 64, 69], 45), ([60, 64, 67, 72], 48)]

# Intro (0 – 7.5): tense pad + ticking hats + sparse plucks
for b in range(14):
    t0 = b * BEAT
    place(music, t0, hat(), 0.5 if b % 2 else 0.8, pan=0.3)
    place(music, t0 + BEAT / 2, hat(), 0.35, pan=-0.3)
place(music, 0.0, pad_chord([57, 60, 64], 4.0, 1400), 0.9)
place(music, 4.0, pad_chord([53, 57, 60], 3.6, 700), 0.9)    # darker on "pain"
for k, n in enumerate([69, 72, 76, 72, 69, 72, 76, 79]):
    place(music, 0.25 + k * BEAT, pluck(note(n)), 0.22, pan=(-0.4 if k % 2 else 0.4))
# low pulse during pain
for b in range(7):
    place(music, 4.0 + b * BEAT, bass(note(33), BEAT * 0.9), 0.35)

# Riser 6.0 – 7.45
d = 1.45; t = t_arr(d)
nz = rng.standard_normal(len(t))
riser = onepole_lp(nz, 300 + 9000 * (t / d) ** 2) * (t / d) ** 1.6 * 0.9
ph = 2 * np.pi * np.cumsum(220 * 2 ** (2 * t / d)) / SR
riser += np.sin(ph) * (t / d) ** 2 * 0.12
place(sfx, 6.0, riser, 0.55)

# Drop (7.5 – 34.0)
DROP = 7.5
END_MUSIC = 34.25
nbeats = int((END_MUSIC - DROP) / BEAT)
for b in range(nbeats):
    t0 = DROP + b * BEAT
    bar = b // 4
    chord, root = PROG[bar % 4]
    # breakdown: 1 bar of lighter drums before CTA (≈ 28.9–30.0) for lift
    in_break = 28.9 <= t0 < 30.0
    if not in_break:
        place(music, t0, kick(), 0.95)
    if b % 4 in (1, 3) and not in_break:
        place(music, t0, clap(), 0.55)
    place(music, t0 + BEAT / 2, hat(open_=(b % 2 == 1)), 0.55, pan=0.25)
    place(music, t0, hat(), 0.25, pan=-0.25)
    if b % 4 == 0:
        place(music, t0, pad_chord(chord, BAR + 0.1, 2600), 1.0)
    # bass: root on 1 and the "and" of 2
    if b % 4 in (0, 2):
        place(music, t0, bass(note(root), BEAT * 0.95), 0.55)
    if b % 4 in (1, 3):
        place(music, t0 + BEAT / 2, bass(note(root + 12), BEAT * 0.45), 0.35)
    # arpeggio pluck 8ths
    arp = [chord[0] + 12, chord[1] + 12, chord[2] + 12, chord[1] + 12]
    for h in range(2):
        nn = arp[(b * 2 + h) % 4]
        place(music, t0 + h * BEAT / 2, pluck(note(nn), 0.3), 0.18, pan=(0.35 if h else -0.35))

# final chord swell under CTA
place(music, 30.0, pad_chord([60, 64, 67, 72, 76], 4.4, 3200), 0.9)

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


# S1
place(sfx, 0.12, whoosh(0.45), 0.35)
for i in range(5):
    place(sfx, 1.0 + i * 0.42, notif(note(76 + i * 2)), 0.32, pan=(-0.3 + i * 0.15))
# S2
place(sfx, 4.0, impact(False), 0.35)
for i in range(5):
    place(sfx, 5.4 + i * 0.28, whoosh(0.4, up=False), 0.18, pan=-0.6)
# S3 reveal
place(sfx, 7.42, impact(True), 0.8)
place(sfx, 7.55, buzz(1.0, 200), 0.35, pan=0.2)
place(sfx, 8.05, sparkle(), 0.5)
# S4 chat
place(sfx, 11.3, whoosh(0.5), 0.4)
place(sfx, 12.35, recv(), 0.45, pan=0.25)
place(sfx, 12.5, buzz(1.4, 230), 0.25, pan=-0.3)
place(sfx, 13.95, send(), 0.5, pan=-0.25)
place(sfx, 15.5, recv(), 0.45, pan=0.25)
place(sfx, 16.7, send(), 0.5, pan=-0.25)
place(sfx, 18.0, chime_cash(), 0.55)
# S5
place(sfx, 20.25, whoosh(0.5), 0.4)
place(sfx, 21.15, pop(500, 1200, 0.12), 0.45)
place(sfx, 21.7, buzz(0.7, 240), 0.3, pan=0.4)
place(sfx, 22.6, scratch(), 0.35)
place(sfx, 23.05, thud(), 0.85)
for i in range(3):
    place(sfx, 23.55 + i * 0.35, tick(), 0.5)
# S6
place(sfx, 25.65, whoosh(0.5), 0.4)
place(sfx, 26.4, pop(600, 1100, 0.1), 0.35)
place(sfx, 26.7, buzz(0.6, 210), 0.25)
place(sfx, 27.0, pop(600, 1100, 0.1), 0.35)
for i in range(4):
    place(sfx, 27.3 + i * 0.22, tick(), 0.4, pan=(-0.5 if i % 2 else 0.5))
# S7
place(sfx, 29.62, whoosh(0.5), 0.45)
place(sfx, 30.0, impact(True), 0.6)
place(sfx, 30.05, buzz(0.9, 200), 0.3)
place(sfx, 31.3, pop(500, 1300, 0.12), 0.45)
place(sfx, 32.1, sparkle(), 0.4)

# ------------------------------------------------------------------ mix
def norm_peak(x, peak):
    m = np.max(np.abs(x)) + 1e-9
    return x * (peak / m)

music = norm_peak(music, 0.42)
sfx = norm_peak(sfx, 0.62)
mix = music + sfx
# fades
t = np.arange(N) / SR
mix *= np.minimum(1, t / 0.08)[:, None]
fade = np.clip((DUR - t) / 0.9, 0, 1)
mix *= fade[:, None]
# soft clip
mix = np.tanh(mix * 1.15) / np.tanh(1.15)
mix = norm_peak(mix, 0.93)
pcm = (mix * 32767).astype(np.int16)
with wave.open('/tmp/nahla-video/audio.wav', 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes(pcm.tobytes())
print('ok', pcm.shape)
