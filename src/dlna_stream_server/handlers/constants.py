FFMPEG_MP3_PARAMS = [
    "ffmpeg",
    "-rw_timeout", "10000000",
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "1",
    "-thread_queue_size", "4096",
    "-analyzeduration", "500000",
    "-probesize", "40M",
    "-fflags", "+genpts+fastseek",
    "-flags", "low_delay",
    "-i", "{yandex_url}",
    "-map_metadata", "-1",
    "-c:a", "copy",
    "-bufsize", "40M",
    "-max_muxing_queue_size", "8192",
    "-maxrate", "3840k",
    "-f", "mp3",
    "pipe:1"
]

# Параметры FFmpeg для локальных MP3 файлов (без стриминговых опций)
FFMPEG_LOCAL_MP3_PARAMS = [
    "ffmpeg",
    "-re",
    "-fflags", "+flush_packets",
    "-flush_packets", "1",
    "-i", "{yandex_url}",
    "-map", "0:a",
    "-map_metadata", "-1",
    "-id3v2_version", "0",
    "-write_id3v1", "0",
    "-acodec", "copy",
    "-avoid_negative_ts", "make_zero",
    "-f", "mp3",
    "pipe:1"
]

FFMPEG_AAC_PARAMS = [
    "ffmpeg",
    "-re",
    "-rw_timeout", "5000000",
    "-analyzeduration", "3000000",
    "-probesize", "3M",
    "-thread_queue_size", "4096",
    "-multiple_requests", "1",
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "10",
    "-i", "{yandex_url}",
    "-vn",
    "-c:a", "aac",
    "-bsf:a", "aac_adtstoasc",
    "-f", "adts",
    "-bufsize", "2M",
    "pipe:1"
]
