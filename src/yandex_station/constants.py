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
