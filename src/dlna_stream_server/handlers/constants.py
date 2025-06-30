FFMPEG_MP3_PARAMS = [
    "ffmpeg",
    "-re",
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
    "-analyzeduration", "1000000",
    "-probesize", "1M",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-i", "{yandex_url}",
    "-vn",
    "-c:a", "copy",
    "-bsf:a", "aac_adtstoasc",
    "-f", "adts",
    "-"
]

FFMPEG_STABLE_PARAMS = [
    "ffmpeg",
    "-user_agent", "Mozilla/5.0",
    "-headers", "Accept: */*",
    "-multiple_requests", "1",
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "2",
    "-reconnect_at_eof", "1",
    "-timeout", "10000000",
    "-analyzeduration", "2M",
    "-probesize", "2M",
    "-i", "{yandex_url}",
    "-acodec", "libmp3lame",
    "-b:a", "320k",
    "-f", "mp3",
    "-avoid_negative_ts", "make_zero",
    "-fflags", "+genpts",
    "-max_muxing_queue_size", "1024",
    "pipe:1"
]


FFMPEG_FAST_PREBUFFERED_PARAMS = [
    "ffmpeg",
    # Параметры запроса
    "-user_agent", "Mozilla/5.0",
    "-headers", "Accept: */*",
    "-multiple_requests", "1",
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "2",
    "-reconnect_at_eof", "1",

    # Забираем весь файл как можно быстрее
    "-i", "{yandex_url}",
    "-analyzeduration", "10000000",     # анализируем весь поток
    "-probesize", "10000000",           # и глубоко
    "-bufsize", "50M",                  # огромный буфер

    # Против задержек
    "-fflags", "+nobuffer",
    "-flush_packets", "0",

    # Перекодируем (стабильность выше)
    "-acodec", "libmp3lame",
    "-b:a", "320k",
    "-f", "mp3",

    "-max_muxing_queue_size", "4096",
    "pipe:1"
]
