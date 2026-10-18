# Piper voice models by target language

ProntoFix chooses a Piper voice using the selected target-language code. The model files must be installed on the server; the English model alone cannot synthesize Russian, Ukrainian, or other languages.

## Configuration

Set `PIPER_MODELS` in the systemd service environment as a JSON object mapping the language code used by ProntoFix to the absolute path of that language's Piper `.onnx` file.

Example for English and Russian (replace the Russian path with the actual installed model file):

```ini
[Service]
Environment="PIPER_MODELS={\"en\":\"/mnt/ProntoFix/admin/voices/en_US-lessac-medium.onnx\",\"ru\":\"/mnt/ProntoFix/admin/voices/ru_RU-VOICE-medium.onnx\"}"
```

The example Russian filename is a placeholder, not a downloadable model name. Use the exact filename that exists on disk. Keep paths JSON-escaped inside the systemd quoted value.

For example, create or edit:
`/etc/systemd/system/prontofix.service.d/tts.conf`

Then run:

```bash
systemctl daemon-reload
systemctl restart prontofix
curl -s http://127.0.0.1:8000/api/health
```

The health response includes `tts_languages`, which lists language codes whose model paths exist. For each target language, add its own model path. Codes supported by the interface include `uk, ru, de, fr, it, en, ar, pt, es, bg, pl, cs, sk, lt, lv, no`.

When the chosen target language has no configured model, ProntoFix keeps the transcript/subtitles and reports a voice-model warning; it cannot generate a dubbed MP4 until that language's voice is installed.
