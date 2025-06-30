FFMPEG_MP3_PARAMS = [
    "ffmpeg",
    "-rw_timeout", "5000000",
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "2",
    "-thread_queue_size", "512",
    "-analyzeduration", "0",
    "-probesize", "32",
    "-i", "{yandex_url}",
    "-map_metadata", "-1",
    "-acodec", "libmp3lame",
    "-b:a", "320k",
    "-bufsize", "1M",
    "-f", "mp3",
    "pipe:1"
]

FFMPEG_AAC_PARAMS = [
    "ffmpeg",
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
