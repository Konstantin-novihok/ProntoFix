from __future__ import annotations
import json, os, shutil, sqlite3, subprocess, threading, time, uuid, tempfile
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
WEB_DIR=Path(__file__).resolve().parent.parent/"web"
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
        import argostranslate.translate as argos
        languages={lang.code:lang for lang in argos.get_installed_languages()}
        from_lang=languages.get(src)
        to_lang=languages.get(dst)
        if from_lang is None or to_lang is None:
            installed=", ".join(sorted(languages))
            raise RuntimeError(f"Язык {src if from_lang is None else dst} не найден среди установленных Argos. Доступны: {installed}")
        translation=next((tr for tr in from_lang.translations_from if tr.to_lang.code==dst),None)
        if translation is None:
            raise RuntimeError(f"Не установлена языковая пара Argos Translate: {src} → {dst}.")
        out=translation.translate(text)
        if out and out.strip():return out.strip()
        raise RuntimeError(f"Argos Translate вернул пустой перевод для {src} → {dst}.")
    except Exception as e:
        if isinstance(e,RuntimeError):raise
        raise RuntimeError(f"Ошибка Argos Translate при переводе {src} → {dst}: {type(e).__name__}: {e}") from e
def duration(path):
    out=cmd(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)])
    return max(.1,float(out.stdout.strip()))

def piper_models():
    try:
        configured=json.loads(os.getenv("PIPER_MODELS","{}"))
        if not isinstance(configured,dict): configured={}
    except (TypeError,json.JSONDecodeError): configured={}
    if PIPER_MODEL: configured.setdefault("en",PIPER_MODEL)
    return {str(k):str(v) for k,v in configured.items() if v}

def make_dub(rows, model_path, work, video_path):
    clips=[]
    for i,row in enumerate(rows):
        text=row["text"].strip()
        if not text: continue
        raw=work/f"speech_{i:04d}.wav"
        proc=subprocess.run([PIPER,"--model",model_path,"--output_file",str(raw)],input=text,text=True,capture_output=True)
        if proc.returncode:
            detail=(proc.stderr or proc.stdout or "Piper завершился с ошибкой").strip()
            raise RuntimeError(f"Синтез сегмента {i+1} не удался: {detail[-1200:]}")
        start=max(0.0,float(row["start"])); end=max(start+0.12,float(row["end"]))
        clips.append((raw,start,end))
    if not clips: raise RuntimeError("Нет текста для синтеза речи.")
    graph=[]; labels=[]; args=["ffmpeg","-nostdin","-y","-i",str(video_path)]
    for path,start,end in clips: args += ["-i",str(path)]
    for n,(path,start,end) in enumerate(clips, start=1):
        target=max(.12,end-start)
        # Never cut synthesized speech to the original segment duration: translated
        # text can be longer. Apply only moderate acceleration and let any remaining
        # speech finish naturally; later segments may overlap slightly rather than vanish.
        ratio=duration(path)/target
        tempos=[]
        while ratio>1.35:
            tempos.append(1.35)
            ratio/=1.35
        if ratio>1.0:
            tempos.append(ratio)
        filters=["aresample=48000","asetpts=PTS-STARTPTS"]
        filters.extend(f"atempo={x:.6f}" for x in tempos)
        filters += [f"adelay={round(start*1000)}:all=1"]
        chain=",".join(filters)
        label=f"a{n}"; graph.append(f"[{n}:a]{chain}[{label}]"); labels.append(f"[{label}]")
    # Keep the original soundtrack as ambience/music and mix translated speech over it.
    # This ducks the original track during speech; perfect voice removal requires source separation.
    graph.append("[0:a]aresample=48000,asetpts=PTS-STARTPTS,volume=0.30[bg];" + "".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,alimiter=limit=0.95[voice];" + "[bg][voice]sidechaincompress=threshold=0.025:ratio=8:attack=15:release=350[ducked];" + "[ducked][voice]amix=inputs=2:duration=longest:weights='1 1':normalize=0,alimiter=limit=0.95[dub]")
    out=work/"video_translated_audio.mp4"
    output_duration=max(duration(video_path),max(start+duration(path) for path,start,end in clips))
    args += ["-filter_complex",";".join(graph),"-map","0:v:0","-map","[dub]","-c:v","libx264","-preset","veryfast","-crf","23","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-t",f"{output_duration:.3f}","-shortest","-movflags","+faststart",str(out)]
    cmd(args)
    audio=work/"translated.wav"
    cmd(["ffmpeg","-nostdin","-y","-i",str(out),"-vn","-ac","1","-ar","22050","-c:a","pcm_s16le",str(audio)])
    return audio,out
def process(j):
    jid=j["id"]; src=Path(j["input_path"]); work=JOBS/jid; work.mkdir(parents=True,exist_ok=True)
    # Intermediate audio and synthesis chunks live in RAM-backed tmpfs when there is
    # enough room. Fall back to the system temp directory rather than risking ENOSPC.
    ram_root=Path("/dev/shm")
    source_size=src.stat().st_size if src.is_file() else 0
    try:
        ram_free=shutil.disk_usage(ram_root).free if ram_root.is_dir() else 0
    except OSError:
        ram_free=0
    temp_root=str(ram_root) if ram_free >= max(512*1024*1024, source_size*2) else None
    try:
        with tempfile.TemporaryDirectory(prefix=f"prontofix-{jid[:8]}-", dir=temp_root) as temp_dir:
            scratch=Path(temp_dir)
            process_in_ram(j, src, work, scratch)
    except Exception as e:
        update(jid,status="error",progress=0,message=str(e))

def process_in_ram(j, src, work, scratch):
    jid=j["id"]
    try:
        update(jid,status="processing",progress=2,message="Извлечение аудио")
        wav=scratch/"source.wav"; cmd(["ffmpeg","-nostdin","-y","-i",str(src),"-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",str(wav)])
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
        models=piper_models()
        if shutil.which(PIPER) and target_lang in models and Path(models[target_lang]).is_file():
            update(jid,progress=84,message=f"Синтез речи: {LANGS.get(target_lang,target_lang)}")
            voice_tmp,out_tmp=make_dub(translated,models[target_lang],scratch,src)
            # Keep the completed video next to the uploaded original; publish a copy
            # of the audio artifact in the durable per-job result directory.
            final_video=src.parent/(src.stem+"_ProntoFix_"+jid[:8]+".mp4")
            final_audio=work/"translated.wav"
            shutil.copy2(out_tmp,final_video)
            shutil.copy2(voice_tmp,final_audio)
            result.update({"translated_audio":str(final_audio),"dubbed_video":str(final_video),"tts_voice_language":target_lang,"dubbing_mode":"segment_aligned","processing_buffer":"RAM (/dev/shm)" if scratch.parent==Path("/dev/shm") else "temporary disk (RAM buffer had insufficient free space)"})
        else:
            available=", ".join(sorted(k for k,p in models.items() if Path(p).is_file())) or "нет"
            result["tts_warning"]=f"Нет настроенной голосовой модели Piper для языка {target_lang}. Настроены языки: {available}."
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
def health():
    models=piper_models()
    return {"status":"ok","languages":LANGS,"whisper_model":MODEL,"tts_enabled":bool(shutil.which(PIPER) and any(Path(p).is_file() for p in models.values())),"tts_languages":sorted(k for k,p in models.items() if Path(p).is_file())}
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
@app.delete("/api/jobs/{job_id}")
def delete_job(job_id:str):
    j=getjob(job_id)
    if not j:
        raise HTTPException(404,"Задача не найдена")
    if j["status"]=="processing":
        raise HTTPException(409,"Нельзя удалить задачу во время обработки")
    input_path=Path(j["input_path"]).resolve()
    incoming=(DATA/"incoming").resolve()
    if input_path.parent==incoming and input_path.is_file():
        input_path.unlink(missing_ok=True)
    # Remove only this job's predictable MP4 stored beside the source.
    result_video=j.get("result",{}).get("dubbed_video")
    if result_video:
        output_path=Path(result_video).resolve()
        expected_name=input_path.stem+"_ProntoFix_"+job_id[:8]+".mp4"
        if output_path.parent==input_path.parent and output_path.name==expected_name:
            output_path.unlink(missing_ok=True)
    work=(JOBS/job_id).resolve()
    if work.parent==JOBS.resolve() and work.is_dir():
        shutil.rmtree(work,ignore_errors=True)
    with dbconn() as c:
        c.execute("DELETE FROM jobs WHERE id=?",(job_id,))
    return {"status":"deleted","id":job_id}

@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id:str):
    j=getjob(job_id)
    if not j: raise HTTPException(404,"Задача не найдена")
    if j["status"] in ("queued","processing"): raise HTTPException(409,"Задача уже в очереди или обрабатывается")
    source=Path(j["input_path"]).resolve()
    if not source.is_file(): raise HTTPException(404,"Исходный видеофайл отсутствует на сервере")
    work=(JOBS/job_id).resolve()
    if work.parent==JOBS.resolve() and work.is_dir():
        for child in work.iterdir():
            if child.name=="thumbnail.jpg": continue
            if child.is_dir(): shutil.rmtree(child,ignore_errors=True)
            else: child.unlink(missing_ok=True)
    with dbconn() as c: c.execute("UPDATE jobs SET status='queued',progress=0,message='Повторно поставлено в очередь',result_json='{}',updated=? WHERE id=?",(time.time(),job_id))
    return {"id":job_id,"status":"queued"}

@app.get("/api/jobs/{job_id}/thumbnail")
def thumbnail(job_id:str):
    j=getjob(job_id)
    if not j: raise HTTPException(404,"Задача не найдена")
    thumb=JOBS/job_id/"thumbnail.jpg"
    if not thumb.is_file():
        source=Path(j["input_path"])
        if not source.is_file(): raise HTTPException(404,"Исходное видео отсутствует")
        thumb.parent.mkdir(parents=True,exist_ok=True)
        try: subprocess.run(["ffmpeg","-nostdin","-y","-ss","1","-i",str(source),"-frames:v","1","-vf","scale=480:-2","-q:v","4",str(thumb)],check=True,capture_output=True,timeout=30)
        except Exception: raise HTTPException(404,"Не удалось создать миниатюру видео")
    return FileResponse(thumb,media_type="image/jpeg",headers={"Cache-Control":"no-store"})
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
    media_types={"dubbed-video":"video/mp4","translated-audio":"audio/wav","transcript":"text/plain; charset=utf-8","translated-transcript":"text/plain; charset=utf-8","original-srt":"application/x-subrip; charset=utf-8","translated-srt":"application/x-subrip; charset=utf-8"}
    return FileResponse(path,filename=Path(path).name,media_type=media_types.get(kind,"application/octet-stream"),content_disposition_type="inline" if kind=="dubbed-video" else "attachment")
@app.patch("/api/jobs/{job_id}/transcript")
def edit(job_id:str,data:TranscriptEdit):
    j=getjob(job_id)
    if not j:raise HTTPException(404,"Задача не найдена")
    path=j.get("result",{}).get("translated_transcript" if data.translated else "transcript")
    if not path:raise HTTPException(409,"Текст ещё не готов")
    Path(path).write_text(data.text,encoding="utf-8"); return {"status":"saved"}

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
