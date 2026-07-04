ALICE_ACTIVE_STATES = {"LISTENING", "SPEAKING", "BUSY"}

RUARK_IDLE_VOLUME = 2

FADE_TIME = 1

SOCKET_RECONNECT_DELAY = 1

STREAMING_RESTART_DELAY = 1

# Период опроса состояния станции в цикле стриминга, секунды
STREAM_POLL_INTERVAL = 0.5

# Скачок прогресса трека больше этого порога считается разрывом
# (повтор, перемотка) и требует ресинка локального стрима, секунды
PROGRESS_JUMP_THRESHOLD = 5.0
