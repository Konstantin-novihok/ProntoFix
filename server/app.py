from __future__ import annotations
import json, os, shutil, sqlite3, subprocess, threading, time, uuid
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA=Path(os.getenv("PRONTOFIX_DATA","/data")).resolve()
UPLOADS=DATA/"uploads"; JOBS=DATA/"jobs"; DB=DATA/"prontofix.sqlite3"
MODEL=os.getenv("WHISPER_MODEL","small"); THREADS=max(1,int(os.getenv("CPU_THREADS","4")))
PIPER=os.getenv("PIPER_BIN","piper"); PIPER_MODEL=os.getenv("PIPER_MODEL","")
LANGS={"uk":"Українська","ru":"Русский","de":"Deutsch","fr":"Français","it":"Italiano","en":"English","ar":"العربية","pt":"Português","es":"Español","bg":"Български","pl":"Polski","cs":"Čeština","sk":"Slovenčina","lt":"Lietuvių","lv":"Latviešu","no":"Norsk"}
app=FastAPI(title="ProntoFix API",version="0.1.0")
WEB_DIR=Path("/app/web")
app.add_middleware(CORSMiddleware,allow_origins=os.getenv("CORS_ORIGINS","*").split(","),allow_credentials=False,allow_methods=["*"],allow_headers=["*"])

def dbconn():
    DATA.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; return c
def init_db():
    with dbconn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,filename TEXT,input_path TEXT,source_lang TEXT,target_lang TEXT,status TEXT,progress INTEGER DEFAULT 0,message TEXT DEFAULT '',created REAL,updated REAL,result_json TEXT DEFAULT '{}')""")
def update(job_id,**v):
    v["updated"]=time.time()
    with dbconn() as c: c.execute("UPDATE jobs SET "+", ".join(k+"=?" for k in v)+" WHERE id=?",(*v.values(),job_id))
def getjob(jid):
    with dbconn() as c: r=c.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone()
    if not r:return None
    x=dict(r); x["result"]=json.loads(x.pop("result_json") or "{}"); return x
def cmd(args):
    try:return subprocess.run(args,check=True,capture_output=True,text=True)
    except FileNotFoundError as e: raise RuntimeError("Не найдена команда "+args[0]+".") from e
    except subprocess.CalledProcessError as e: raise RuntimeError((e.stderr or e.stdout or str(e))[-2500:]) from e
def stamp(s):
    ms=round(s*1000); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); sec,ms=divmod(ms,1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"
def srt(rows):
    return "\n\n".join(f"{i}\n{stamp(r['start'])} --> {stamp(r['end'])}\n{r['text']}" for i,r in enumerate(rows,1))+"\n"
def translate(text,src,dst):
    if src==dst:return text
    try:
        import argostranslate.translate
        out=argostranslate.translate.translate(text,src,dst)
        if out and out.strip():return out.strip()
    except Exception:pass
    raise RuntimeError(f"Не установлена языковая пара Argos Translate: {src} → {dst}. Установите модель перевода для этого направления.")
def duration(path):
    out=cmd(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)])
    return max(.1,float(out.stdout.strip()))
def process(j):
    jid=j["id"]; src=Path(j["input_path"]); work=JOBS/jid; work.mkdir(parents=True,exist_ok=True)
    try:
        update(jid,status="processing",progress=2,message="Извлечение аудио")
        wav=work/"source.wav"; cmd(["ffmpeg","-nostdin","-y","-i",str(src),"-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",str(wav)])
        from faster_whisper import WhisperModel
        update(jid,progress=8,message="Распознавание на CPU")
        model=WhisperModel(MODEL,device="cpu",compute_type="int8",cpu_threads=THREADS,num_workers=1)
        segments,info=model.transcribe(str(wav),language=j["source_lang"] or None,beam_size=5,vad_filter=True)
        rows=[{"start":float(s.start),"end":float(s.end),"text":s.text.strip()} for s in segments if s.text.strip()]
        if not rows:raise RuntimeError("Речь не распознана; проверьте файл и звуковую дорожку.")
        source_lang=j["source_lang"] or info.language or "unknown"; target_lang=j["target_lang"] or source_lang
        orig=work/"transcript.txt"; osrt=work/"subtitles_original.srt"
        orig.write_text("\n".join(r["text"] for r in rows)+"\n",encoding="utf-8"); osrt.write_text(srt(rows),encoding="utf-8")
        update(jid,progress=62,message="Перевод текста")
        translated=[]
        for i,r in enumerate(rows):
            translated.append({**r,"text":translate(r["text"],source_lang,target_lang)})
            if i%20==0:update(jid,progress=min(82,63+int(i/max(1,len(rows))*19)),message=f"Перевод сегмента {i+1}/{len(rows)}")
        txt=work/"transcript_translated.txt"; subs=work/"subtitles_translated.srt"
        txt.write_text("\n".join(r["text"] for r in translated)+"\n",encoding="utf-8"); subs.write_text(srt(translated),encoding="utf-8")
        result={"source_language":source_lang,"detected_language":info.language,"target_language":target_lang,"segments":len(rows),"transcript":str(orig),"original_srt":str(osrt),"translated_transcript":str(txt),"translated_srt":str(subs)}
        if PIPER_MODEL and shutil.which(PIPER):
            voice=work/"translated.wav"; spoken="\n".join(r["text"] for r in translated)
            subprocess.run([PIPER,"--model",PIPER_MODEL,"--output_file",str(voice)],input=spoken,text=True,check=True,capture_output=True)
            out=work/"video_translated_audio.mp4"; dur=duration(src)
            cmd(["ffmpeg","-nostdin","-y","-i",str(src),"-i",str(voice),"-map","0:v:0","-map","1:a:0","-c:v","copy","-c:a","aac","-af","apad","-t",f"{dur:.3f}","-movflags","+faststart",str(out)])
            result.update({"translated_audio":str(voice),"dubbed_video":str(out)})
        else:result["tts_warning"]="Для озвучивания установите Piper, совместимую голосовую модель и переменную PIPER_MODEL."
        update(jid,status="done",progress=100,message="Готово",result_json=json.dumps(result,ensure_ascii=False))
    except Exception as e:update(jid,status="error",progress=0,message=str(e))
def worker():
    while True:
        try:
            with dbconn() as c:r=c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if r:
                j=dict(r); update(j["id"],status="processing",progress=1,message="Задача запущена"); process(j)
            else:time.sleep(2)
        except Exception:time.sleep(5)
@app.on_event("startup")
def startup():
    DATA.mkdir(parents=True,exist_ok=True); UPLOADS.mkdir(parents=True,exist_ok=True); JOBS.mkdir(parents=True,exist_ok=True); (DATA/"incoming").mkdir(exist_ok=True)
    init_db(); threading.Thread(target=worker,daemon=True,name="prontofix-worker").start()
class Complete(BaseModel):
    filename:str=Field(min_length=1,max_length=255); total_chunks:int=Field(ge=1,le=100000)
    source_language:str|None=None; target_language:str|None=None
class TranscriptEdit(BaseModel):
    text:str=Field(max_length=20000000); translated:bool=False
@app.get("/api/health")
def health():return {"status":"ok","languages":LANGS,"whisper_model":MODEL,"tts_enabled":bool(PIPER_MODEL)}
@app.get("/api/jobs")
def list_jobs():
    with dbconn() as c:ids=[r["id"] for r in c.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT 100")]
    return [getjob(i) for i in ids]
@app.post("/api/uploads/{upload_id}/chunks/{index}")
async def chunk(upload_id:str,index:int,request:Request):
    if not upload_id.replace("-","").isalnum() or index<0:raise HTTPException(400,"Некорректный идентификатор загрузки")
    body=await request.body()
    if not body or len(body)>32*1024*1024:raise HTTPException(413,"Размер части должен быть до 32 MiB")
    folder=UPLOADS/upload_id; folder.mkdir(parents=True,exist_ok=True); tmp=folder/f"{index}.tmp"; tmp.write_bytes(body); tmp.replace(folder/f"{index}.part")
    return {"upload_id":upload_id,"index":index,"bytes":len(body)}
@app.post("/api/uploads/{upload_id}/complete")
def complete(upload_id:str,data:Complete):
    folder=UPLOADS/upload_id
    if not folder.is_dir():raise HTTPException(404,"Загрузка не найдена")
    name=Path(data.filename.replace("\\","/")).name
    if not name or name in {".",".."}:raise HTTPException(400,"Некорректное имя файла")
    dest=DATA/"incoming"/(uuid.uuid4().hex+"_"+name)
    try:
        with dest.open("wb") as out:
            for i in range(data.total_chunks):
                part=folder/f"{i}.part"
                if not part.is_file():raise HTTPException(400,f"Отсутствует часть {i}")
                with part.open("rb") as f:shutil.copyfileobj(f,out,4*1024*1024)
        shutil.rmtree(folder,ignore_errors=True); jid=uuid.uuid4().hex; now=time.time()
        sl=data.source_language if data.source_language in LANGS else None; tl=data.target_language if data.target_language in LANGS else None
        with dbconn() as c:c.execute("INSERT INTO jobs(id,filename,input_path,source_lang,target_lang,status,progress,message,created,updated) VALUES(?,?,?,?,?,'queued',0,'В очереди',?,?)",(jid,name,str(dest),sl,tl,now,now))
        return {"id":jid,"status":"queued","filename":name}
    except Exception:dest.unlink(missing_ok=True); raise
@app.get("/api/jobs/{job_id}")
def detail(job_id:str):
    j=getjob(job_id)
    if not j:raise HTTPException(404,"Задача не найдена")
    return j
@app.get("/api/jobs/{job_id}/files/{kind}")
def download(job_id:str,kind:str):
    j=getjob(job_id)
    if not j:raise HTTPException(404,"Задача не найдена")
    keys={"transcript":"transcript","original-srt":"original_srt","translated-transcript":"translated_transcript","translated-srt":"translated_srt","translated-audio":"translated_audio","dubbed-video":"dubbed_video"}
    path=j.get("result",{}).get(keys.get(kind,""))
    if not path or not Path(path).is_file():raise HTTPException(404,"Результат пока недоступен")
    return FileResponse(path,filename=Path(path).name)
@app.patch("/api/jobs/{job_id}/transcript")
def edit(job_id:str,data:TranscriptEdit):
    j=getjob(job_id)
    if not j:raise HTTPException(404,"Задача не найдена")
    path=j.get("result",{}).get("translated_transcript" if data.translated else "transcript")
    if not path:raise HTTPException(409,"Текст ещё не готов")
    Path(path).write_text(data.text,encoding="utf-8"); return {"status":"saved"}

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
