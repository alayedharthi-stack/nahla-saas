"""VO v8: opening (comeA) + middle from the original take A (with «نحلة ترد على عميلك بثواني» from bodyA) + closing (end2).
Each segment is cut on a measured pause and level-matched to take A."""
import subprocess, numpy as np, wave, json
def dec(f):
    out=subprocess.run(['ffmpeg','-loglevel','error','-i',f,'-ac','1','-ar','44100','-f','f32le','-'],capture_output=True,check=True).stdout
    return np.frombuffer(out,np.float32).copy()
sr=44100; S=lambda t:int(round(t*sr))
src={'come':dec('vo_dl/comeA.mp3'),'A':dec('vo_dl/takeA.mp3'),'body':dec('vo_dl/bodyA.mp3'),'end':dec('vo_dl/end2.mp3')}
def srms(x): a=x[np.abs(x)>0.01]; return 20*np.log10(np.sqrt((a**2).mean()))
ref=srms(src['A'][S(4.6):S(39.4)])
# (source, start, end, extra dB, silence after)
segs=[('come',0.00,5.70,+0.5,0.15),
      ('A',   4.62,14.05, 0.0,0.15),   # «وكل رسالة…» + «عشان كذا جات نحلة! ذكاء اصطناعي يرد ويبيع… على واتساب أعمالك.»
      ('body',11.10,13.10,0.0,0.00),   # «نحلة ترد على عميلك بثواني،»
      ('A',  15.75,39.40, 0.0,0.20),   # «تعرف منتجاتك… وتقفل البيعة!» … «ومربوطة مباشرة بمتجرك في سلة… وطلباتك.»
      ('end', 0.00,14.80, 0.0,0.00)]   # closing
out=[]; pos=0.0; place={}
fo=S(0.012)
for i,(k,a,b,db,gap) in enumerate(segs):
    x=src[k][S(a):S(b)].copy()
    level=srms(src[k][S(a):S(b)]) if k!='come' else srms(src[k][:S(5.6)])
    x*=10**((ref-level+db)/20)
    x[:fo]*=np.linspace(0,1,fo); x[-fo:]*=np.linspace(1,0,fo)
    place[i]=(k,a,pos)
    out.append(x); pos+=len(x)/sr
    if gap: out.append(np.zeros(S(gap),np.float32)); pos+=gap
comp=np.concatenate(out)
w=wave.open('vo_dl/composite.wav','wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes((np.clip(comp,-1,1)*32767).astype(np.int16).tobytes()); w.close()
json.dump({str(i):v for i,v in place.items()},open('vo_dl/segments_v8.json','w'))
print('composite',round(len(comp)/sr,2)); [print(i,v) for i,v in place.items()]
