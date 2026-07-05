ALICE_ACTIVE_STATES = {"LISTENING", "SPEAKING", "BUSY"}

RUARK_IDLE_VOLUME = 2

FADE_TIME = 1

SOCKET_RECONNECT_DELAY = 1

STREAMING_RESTART_DELAY = 1

# Heartbeat цикла стриминга: максимальное ожидание события станции,
# после которого итерация выполняется принудительно, секунды
STREAM_POLL_INTERVAL = 0.5

# Скачок прогресса трека больше этого порога считается разрывом
# (повтор, перемотка) и требует ресинка локального стрима, секунды
PROGRESS_JUMP_THRESHOLD = 5.0

# Страховка от тишины: как часто опрашивать Ruark и сколько ждать
# после отправки потока, прежде чем считать тишину проблемой, секунды
SILENCE_CHECK_INTERVAL = 3.0
SILENCE_RESEND_GRACE = 15.0

# Кеш прямых ссылок на треки: время жизни записи (CDN-ссылки
# протухают) и максимум записей, секунды / штуки
TRACK_SOURCE_TTL = 600.0
TRACK_SOURCE_CACHE_SIZE = 8

# Локальный TTS: sendText с этим префиксом заставляет Алису
# произнести фразу вслух
TTS_REPEAT_PREFIX = "Повтори за мной"

# Голосовые уведомления об ошибках: сколько падений цикла подряд
# терпеть до объявления и пауза, сбрасывающая счётчик, секунды
STREAMING_FAILURE_ANNOUNCE_AFTER = 3
STREAMING_FAILURE_STREAK_RESET = 60.0
