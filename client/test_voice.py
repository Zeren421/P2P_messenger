from helpers.voice_recorder import VoiceRecorder
import time

rec = VoiceRecorder()
print("Говори 3 секунды...")
rec.start()
time.sleep(3)
path = rec.stop()
print(f"Сохранено: {path}")