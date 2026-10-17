"""ProntoFix: локальное распознавание речи из видео на CPU."""

import argparse
from pathlib import Path

from faster_whisper import WhisperModel


def format_timestamp(seconds: float) -> str:
    """Преобразует секунды в формат SRT: HH:MM:SS,mmm."""
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def transcribe(video_path: Path, model_name: str, language: str | None) -> None:
    if not video_path.is_file():
        raise FileNotFoundError(f"Файл не найден: {video_path}")

    transcript_path = video_path.with_name(f"{video_path.stem}_transcript.txt")
    subtitles_path = video_path.with_name(f"{video_path.stem}_subtitles.srt")

    print(f"ProntoFix: загружаем модель {model_name!r} на CPU...")
    model = WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        num_workers=1,
    )

    print("Распознавание запущено. На CPU процесс может занять продолжительное время.")
    segments, info = model.transcribe(
        str(video_path),
        language=language,
        beam_size=5,
        vad_filter=True,
    )

    segment_count = 0
    with (
        transcript_path.open("w", encoding="utf-8") as transcript,
        subtitles_path.open("w", encoding="utf-8") as subtitles,
    ):
        transcript.write(f"Исходный язык: {info.language}\n")
        transcript.write(f"Вероятность языка: {info.language_probability:.3f}\n\n")

        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue

            segment_count += 1
            transcript.write(text + "\n")
            subtitles.write(
                f"{segment_count}\n"
                f"{format_timestamp(segment.start)} --> "
                f"{format_timestamp(segment.end)}\n"
                f"{text}\n\n"
            )
            print(f"[{format_timestamp(segment.start)}] {text}")

    print("\nРаспознавание завершено.")
    print(f"Сегментов: {segment_count}")
    print(f"Текст: {transcript_path}")
    print(f"Субтитры: {subtitles_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ProntoFix — локальное распознавание речи из видео без GPU."
    )
    parser.add_argument("video", help="Путь к видео- или аудиофайлу")
    parser.add_argument(
        "--language",
        default=None,
        help="Язык исходной речи: en, ru, uk и т. п. По умолчанию определяется автоматически.",
    )
    parser.add_argument(
        "--model",
        choices=("small", "medium"),
        default="small",
        help="Модель распознавания: small для первого теста, medium для сравнения качества.",
    )
    args = parser.parse_args()

    try:
        transcribe(Path(args.video), args.model, args.language)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Ошибка ProntoFix: {error}\n")


if __name__ == "__main__":
    main()
