\# 💬 P2P Messenger



> Децентрализованный мессенджер с E2EE, P2P-соединением и передачей файлов до 50 ГБ.



!\[Version](https://img.shields.io/badge/version-v7.1.1-blue)

!\[Python](https://img.shields.io/badge/python-3.10+-green)

!\[License](https://img.shields.io/badge/license-MIT-orange)



\---



\## ✨ Возможности



\- 🔒 \*\*E2EE\*\* — все сообщения и файлы зашифрованы (X25519 + AES-GCM).

\- 🌐 \*\*P2P\*\* — прямой обмен через UDP (hole punching, STUN). Без центрального сервера.

\- 🚀 \*\*Файлы до 50 ГБ\*\* — 3 ГБ за 2 минуты в локальной сети (\~25 МБ/с).

\- 🎨 \*\*Тёмная тема\*\* — современный UI на PyQt5.

\- 📎 \*\*Отправка файлов\*\* — drag \& drop, скриншоты из буфера (Ctrl+V), миниатюры.

\- 🛡️ \*\*Fallback\*\* — если UDP не пробивается, сообщения идут через relay (ntfy.sh).

\- 📜 \*\*История\*\* — сохраняется локально в `data/history.jsonl`.



\---



\## 🚀 Быстрый старт



\### Требования

\- Python 3.10+

\- Windows / Linux / macOS



\### Установка



```bash

git clone https://github.com/Zeren421/P2P\_messenger.git

cd P2P\_messenger

pip install -r requirements.txt

