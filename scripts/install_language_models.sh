#!/usr/bin/env bash
set -Eeuo pipefail

# Run inside the ProntoFix Ubuntu LXC as root, or with sudo.
APP_DIR="${PRONTOFIX_DIR:-/opt/prontofix}"
VENV="${PRONTOFIX_VENV:-$APP_DIR/.venv}"
VOICE_DIR="${PIPER_VOICE_DIR:-/mnt/ProntoFix/admin/voices}"
LANG_CODES="uk ru de fr it en ar pt es bg pl cs sk lt lv no"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "ERROR: Python venv not found: $VENV" >&2
  exit 1
fi
mkdir -p "$VOICE_DIR" /var/lib/prontofix
LOG="/var/lib/prontofix/language-model-install.log"
exec > >(tee -a "$LOG") 2>&1

echo "=== ProntoFix language model installation: $(date -Is) ==="
echo "App: $APP_DIR"
echo "Voices: $VOICE_DIR"

# Install the downloader's dependencies into the same environment as the API.
"$VENV/bin/python" -m pip install --upgrade argostranslate requests

# Install every available directed Argos package whose source AND target are
# among ProntoFix's 16 UI languages. Argos may still use pivoting for pairs that
# have no direct package. This can take a long time and substantial disk space.
"$VENV/bin/python" - "$LANG_CODES" <<'PY'
import sys, time, traceback
import argostranslate.package

wanted=set(sys.argv[1].split())
print("Updating Argos package index...", flush=True)
argostranslate.package.update_package_index()
packages=argostranslate.package.get_available_packages()
selected=[p for p in packages if p.from_code in wanted and p.to_code in wanted and p.from_code != p.to_code]
print(f"Available direct Argos packages between UI languages: {len(selected)}", flush=True)
installed=argostranslate.package.get_installed_packages()
have={(p.from_code,p.to_code) for p in installed}
failed=[]
for i,p in enumerate(selected,1):
    pair=(p.from_code,p.to_code)
    if pair in have:
        print(f"[{i}/{len(selected)}] already installed {pair[0]}->{pair[1]}", flush=True)
        continue
    print(f"[{i}/{len(selected)}] downloading/installing {pair[0]}->{pair[1]}", flush=True)
    try:
        path=p.download()
        argostranslate.package.install_from_path(path)
    except Exception as e:
        failed.append((pair,repr(e)))
        print(f"FAILED {pair[0]}->{pair[1]}: {e}", flush=True)
    time.sleep(0.2)
print("\nARGOS SUMMARY")
print("Installed/available direct pairs:", len(argostranslate.package.get_installed_packages()))
print("Failed:", len(failed))
for pair,err in failed: print("FAIL",pair,err)
PY

# Piper voice files are published in Hugging Face's rhasspy/piper-voices
# repository. Choose one medium voice when possible, otherwise low/x_low.
# Each voice requires both .onnx and .onnx.json. Preserve model cards/licenses.
"$VENV/bin/python" - "$VOICE_DIR" "$LANG_CODES" <<'PY'
import json, os, sys, urllib.request, urllib.error, pathlib, time
dest=pathlib.Path(sys.argv[1]); wanted=set(sys.argv[2].split())
api="https://huggingface.co/api/models/rhasspy/piper-voices/tree/main?recursive=true&expand=false"
print("\nFetching Piper voice inventory from Hugging Face...", flush=True)
req=urllib.request.Request(api,headers={"User-Agent":"ProntoFix-language-installer/1.0"})
with urllib.request.urlopen(req,timeout=60) as r: entries=json.load(r)
paths=[x.get("path","") for x in entries if x.get("type")=="file"]
# Directory layout: locale/voice/quality/locale-voice-quality.onnx
locales={"uk":"uk_UA","ru":"ru_RU","de":"de_DE","fr":"fr_FR","it":"it_IT",
         "en":"en_US","ar":"ar_JO","pt":"pt_PT","es":"es_ES","bg":"bg_BG",
         "pl":"pl_PL","cs":"cs_CZ","sk":"sk_SK","lt":"lt_LT","lv":"lv_LV","no":"no_NO"}
rank={"medium":0,"low":1,"x_low":2,"high":3}
installed=[]; missing=[]
for code in sorted(wanted):
    locale=locales[code]
    candidates=[]
    prefix=locale+"/"
    for p in paths:
        if not p.startswith(prefix) or not p.endswith(".onnx"): continue
        parts=p.split("/")
        if len(parts)!=4: continue
        quality=parts[2]
        if quality not in rank: continue
        candidates.append((rank[quality],p))
    candidates.sort()
    if not candidates:
        missing.append((code,locale))
        print(f"NO PIPER VOICE published for {code} ({locale})",flush=True)
        continue
    # One default voice per UI language, medium preferred. If a voice already
    # exists and its ONNX/config pair is complete, leave it untouched.
    chosen=candidates[0][1]
    rel=pathlib.PurePosixPath(chosen)
    base=rel.name[:-5]
    folder=dest/locale/rel.parts[1]/rel.parts[2]
    folder.mkdir(parents=True,exist_ok=True)
    files=[(chosen,folder/(base+".onnx")),(chosen+".json",folder/(base+".onnx.json"))]
    ok=True
    for remote,local in files:
        if local.is_file() and local.stat().st_size>1000: continue
        url="https://huggingface.co/rhasspy/piper-voices/resolve/main/"+remote
        print("Downloading",url,flush=True)
        try:
            request=urllib.request.Request(url,headers={"User-Agent":"ProntoFix-language-installer/1.0"})
            with urllib.request.urlopen(request,timeout=180) as response, open(str(local)+".part","wb") as out:
                while True:
                    block=response.read(1024*1024)
                    if not block: break
                    out.write(block)
            os.replace(str(local)+".part",local)
        except Exception as e:
            ok=False
            print(f"FAILED {remote}: {e}",flush=True)
            try: pathlib.Path(str(local)+".part").unlink()
            except FileNotFoundError: pass
            break
    if ok:
        # Model card is in the same voice directory; download it for license review.
        card=folder/"MODEL_CARD"
        if not card.exists():
            card_url="https://huggingface.co/rhasspy/piper-voices/resolve/main/"+str(rel.parent)+"/MODEL_CARD"
            try:
                request=urllib.request.Request(card_url,headers={"User-Agent":"ProntoFix-language-installer/1.0"})
                with urllib.request.urlopen(request,timeout=60) as response, open(card,"wb") as out: out.write(response.read())
            except Exception as e: print(f"NOTICE: couldn't fetch model card for {base}: {e}",flush=True)
        installed.append((code,str(folder/base)))
        print(f"VOICE OK {code}: {folder/base}",flush=True)
print("\nPIPER SUMMARY")
for code,path in installed: print("VOICE",code,path)
for code,locale in missing: print("UNSUPPORTED",code,locale)
print(f"Usable voices installed: {len(installed)} / {len(wanted)} UI languages")
print("Piper does not currently publish a compatible voice for every ProntoFix UI language; do not map another language's voice as a substitute.")
PY

# Create a JSON map that the ProntoFix service can consume as PIPER_MODELS.
"$VENV/bin/python" - "$VOICE_DIR" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1]); mapping={}
for code,locale in {"uk":"uk_UA","ru":"ru_RU","de":"de_DE","fr":"fr_FR","it":"it_IT","en":"en_US","ar":"ar_JO","pt":"pt_PT","es":"es_ES","bg":"bg_BG","pl":"pl_PL","cs":"cs_CZ","sk":"sk_SK","lt":"lt_LT","lv":"lv_LV","no":"no_NO"}.items():
    candidates=sorted(root.glob(locale+"/*/*/*.onnx"))
    if candidates:
        model=candidates[0]
        if pathlib.Path(str(model)+".json").exists(): mapping[code]=str(model)
path=pathlib.Path("/var/lib/prontofix/piper-models.json")
path.write_text(json.dumps(mapping,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("\nPIPER_MODELS map written:",path)
print(json.dumps(mapping,ensure_ascii=False,indent=2))
PY

echo
echo "Installation finished: $(date -Is)"
echo "Log: $LOG"
echo "IMPORTANT: inspect failures and free disk space before restarting the service."
