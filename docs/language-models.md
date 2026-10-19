# ProntoFix language models and voices

## What this installer does

Run `scripts/install_language_models.sh` **inside the Ubuntu LXC that runs ProntoFix**, not on the TrueNAS host.

- Installs all Argos Translate packages currently indexed whose source and target are both in the 16 UI languages: `uk ru de fr it en ar pt es bg pl cs sk lt lv no`. This is every available directed pair among those codes, not a guarantee that all 16×15 pairs exist. Argos can pivot through an intermediate language when the necessary pair is installed, with a possible quality loss.
- Paginates through the complete Hugging Face Piper voice catalog rather than reading only the first API page.
- Downloads one Piper voice per language when available, preferring medium quality, then low, x_low, and high. English, Portuguese, and Spanish may use a regional variant when the configured region is unavailable.
- Downloads both ONNX and JSON config files and attempts to preserve the model card for license review.
- Writes the detected Piper model paths to `/var/lib/prontofix/piper-models.json` and logs the run to `/var/lib/prontofix/language-model-install.log`.

Official references:
- Argos Translate and model installation: https://github.com/argosopentech/argos-translate
- Argos package index: https://www.argosopentech.com/argospm/index/
- Piper voice catalog and model/config requirements: https://github.com/rhasspy/piper/blob/master/VOICES.md (catalog availability may change).

## Run

```bash
cd /opt/prontofix
git pull origin main
chmod +x scripts/install_language_models.sh
sudo ./scripts/install_language_models.sh
```

The installer expects the virtual environment at `/opt/prontofix/.venv`. Override with `PRONTOFIX_VENV=/path/to/venv` if needed. Voice models default to `/mnt/ProntoFix/admin/voices`; override with `PIPER_VOICE_DIR=/path`.

This is a potentially large download. Check free space first with `df -h`. The script deliberately does not download every voice variant or every Argos language in existence; it selects the available Argos pairs for ProntoFix's 16 languages and one default Piper voice per supported language.

## Voice coverage and regional variants

The Piper catalog changes over time. The installer discovers the current catalog at run time and reports any languages for which it cannot find a compatible voice. It does not treat the UI locale (for example, `en_US`) as the first directory in the repository path: current model paths use the structure `language/locale/voice/quality/model.onnx`. English, Portuguese, and Spanish can use a regional variant (such as `en_GB` or `pt_BR`) if the configured default locale is not available. Other languages require the configured locale. Missing voices are reported instead of silently substituting an unrelated language.

Speech recognition is provided by multilingual faster-whisper; it does not require a separate Whisper model for each UI language. Argos translation and Piper speech synthesis are separate capabilities and must be checked independently.

## After installation

Review the summary and any `FAILED` lines in the log. Confirm that every entry in `/var/lib/prontofix/piper-models.json` points to an existing ONNX file with its matching `.onnx.json` configuration. Then configure the service's `PIPER_MODELS` environment variable from the generated JSON map (the existing application reads this variable as JSON), restart ProntoFix, and verify the health endpoint. The script does not edit systemd configuration or restart the application automatically, to avoid overwriting a manually maintained service environment.

Model downloads can have individual licenses. Review each downloaded `MODEL_CARD` before redistributing voices.
