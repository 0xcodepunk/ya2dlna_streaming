FFMPEG_MP3_PARAMS = [
    "ffmpeg",
    # Входные параметры
    "-re",  # Читаем файл с реальной скоростью
    "-user_agent",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "-headers", "Accept: */*",
    "-multiple_requests", "1",
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "2",
    "-reconnect_at_eof", "1",
    "-timeout", "10000000",
    "-i", "{yandex_url}",
    # Выходные параметры
    "-acodec", "libmp3lame",
    "-b:a", "320k",
    "-f", "mp3",
    "-avoid_negative_ts", "make_zero",
    "-fflags", "+genpts",
    "-max_muxing_queue_size", "1024",
    "pipe:1"
]


FFMPEG_AAC_PARAMS = [
    "ffmpeg",
    "-re",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-analyzeduration", "0",
    "-probesize", "32",
    "-i", "{yandex_url}",
    "-vn",
    "-c:a", "copy",
    "-bsf:a", "aac_adtstoasc",
    "-f", "adts",
    "-"
]
