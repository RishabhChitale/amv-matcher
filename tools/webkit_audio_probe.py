"""Throwaway WebKit (pywebview) probe for the audio sync loop: plays a synthetic video + song for N seconds and
dumps the audio seek/rate log (window.__aud). Usage: AMV_DATA_DIR=<tmp> python tools/webkit_audio_probe.py VIDEO SONG OUT.json [secs]"""
import json, os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import webview
import app_main

video, song, out = (sys.argv[1:4] + ['', '', ''])[:3]; secs = float(sys.argv[4]) if len(sys.argv) > 4 else 10
_start = webview.start

def start(bind=None, **kw):
    def wrapped(*a):
        w = webview.windows[0]
        t = threading.Thread(target=lambda: bind(*a)); t.start()
        def drive():
            ev = lambda c: w.evaluate_js(c)
            time.sleep(3)
            ev("(async()=>{try{const r=await (await POST('/api/register-paths',{paths:%s})).json();const f=p=>({name:p.split('/').pop(),nativePath:p,path:p});enqueue(()=>doVideo(f(r.videos[0])));enqueue(()=>doSongs(r.songs.map(f)));window.__reg=JSON.stringify(r)}catch(e){window.__reg='ERR '+e}})()" % json.dumps([video, song]))
            for _ in range(90):
                time.sleep(1)
                try:
                    if ev("typeof S!=='undefined' && vid.duration>0"): break
                except Exception: pass
            time.sleep(2)
            ev("selectSong({path:%s,best_offset_s:0,name:'song'})" % json.dumps(song)); time.sleep(4)
            ev("S.offset=0;S.manual=true;seek(0)"); time.sleep(1)
            ev("window.__ev=[];['play','playing','pause','waiting','stalled','seeking','seeked','suspend','error'].forEach(n=>aud.addEventListener(n,()=>__ev.push([Math.round(performance.now()),n,+aud.currentTime.toFixed(2)])));window.__raf=0;(function f(){__raf++;requestAnimationFrame(f)})()")
            ev("setInterval(()=>{if(S.playing){const n=performance.now();vid.currentTime;syncAudio(false)}},16)")  # rAF is suspended while the window is not visible
            ev("window.__pe=[];const _p=aud.play.bind(aud);aud.play=()=>{const r=_p();r.catch(e=>__pe.push(String(e)));return r}")
            ev("document.getElementById('b-play').click()")
            time.sleep(secs)
            res = ev("JSON.stringify({reg: window.__reg, vp: D.videoPath, vd: vid.duration, vsrc: vid.src, verr: vid.error && vid.error.code, ov: document.body.innerText.slice(0,200), dur: S.dur, nres: S.results.length, sel: !!S.sel, pe: window.__pe, asrc: aud.currentSrc, aerr: aud.error&&aud.error.code, playing: S.playing, ev: window.__ev, raf: window.__raf, vis: document.visibilityState,  off: S.offset, st: songTime(), ad: aud.duration, vpaused: vid.paused, seeks: window.__aud && __aud.seeks, log: window.__aud && __aud.log, t: vid.currentTime, at: aud.currentTime, paused: aud.paused, rs: aud.readyState, ua: navigator.userAgent})")
            open(out, "w").write(res or "{}")
            w.destroy()
        threading.Thread(target=drive, daemon=True).start()
    return _start(wrapped, **kw)
webview.start = start

if __name__ == "__main__":
    import multiprocessing; multiprocessing.freeze_support()
    sys.exit(app_main.main(["app_main.py"]))
