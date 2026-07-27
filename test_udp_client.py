import socket
import json
import ssl
import urllib.request
import time
import threading

import stun_client

BASE_TOPIC = "moy_chat_server"
SERVER_TOPIC = BASE_TOPIC + "_server"
CLIENT_TOPIC = BASE_TOPIC + "_client"

# Локальный порт фиксируем, чтобы STUN, публикация и punch шли через
# один и тот же сокет — так же, как это сделано на стороне C++ сервера.
LOCAL_PORT = 9090


def _ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_local_ip() -> str | None:
    """
    Определяет локальный (LAN) IP машины — тот, что используется для
    исходящих соединений в интернет. Пакетов при этом не уходит:
    connect() на UDP-сокете просто выбирает исходящий интерфейс/маршрут.
    """
    tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tmp.connect(("8.8.8.8", 80))
        return tmp.getsockname()[0]
    except Exception:
        return None
    finally:
        tmp.close()


def publish_address(topic: str, ip: str, port: int, local_ip: str | None, local_port: int) -> bool:
    lines = ["SERVER IS RUNNING", f"Address: {ip}:{port}"]
    if local_ip:
        lines.append(f"LocalAddress: {local_ip}:{local_port}")
    message = "\n".join(lines).encode("utf-8")

    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[ERROR] Failed to publish address: {e}")
        return False


def fetch_address(topic: str, max_retries=10, wait=3):
    """Возвращает (address, local_address) — local_address может быть None."""
    ctx = _ssl_context()
    for attempt in range(max_retries):
        try:
            print(f"[INFO] Attempt {attempt + 1}/{max_retries}: fetching from '{topic}'...")
            url = f"https://ntfy.sh/{topic}/json?poll=1&since=all"

            with urllib.request.urlopen(url, timeout=15, context=ctx) as response:
                raw = response.read().decode()

                messages = []
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

                if messages:
                    last_message = messages[-1].get("message", "")
                    address = None
                    local_address = None
                    for line in last_message.split("\n"):
                        stripped = line.strip()
                        # Проверяем сначала LocalAddress:, иначе "Address:"
                        # совпадёт как подстрока внутри "LocalAddress:"
                        if stripped.startswith("LocalAddress:"):
                            local_address = stripped.replace("LocalAddress:", "").strip()
                        elif stripped.startswith("Address:"):
                            address = stripped.replace("Address:", "").strip()

                    if address:
                        return address, local_address

        except Exception as e:
            print(f"[ERROR] Attempt {attempt + 1} failed: {e}")

        if attempt < max_retries - 1:
            print(f"[INFO] Waiting {wait} seconds before next attempt...")
            time.sleep(wait)

    return None, None


def punch_loop(sock: socket.socket, server_ip: str, server_port: int, stop_event: threading.Event):
    """Шлёт пробные пакеты серверу, пока не остановят или не кончатся попытки."""
    for i in range(15):
        if stop_event.is_set():
            return
        try:
            sock.sendto(b"HELLO", (server_ip, server_port))
            print(f"[PUNCH] Sent HELLO #{i + 1} to {server_ip}:{server_port}")
        except Exception as e:
            print(f"[ERROR] Punch send failed: {e}")
        time.sleep(0.5)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", LOCAL_PORT))

    # 1. Свой внешний адрес через STUN (через этот же сокет)
    print("[INFO] Determining own external address via STUN...")
    ext = stun_client.get_external_address_with_fallback(sock)
    if not ext:
        print("[ERROR] STUN request failed. Check your network connection.")
        sock.close()
        return

    my_ip, my_port = ext
    print(f"[OK] My external address: {my_ip}:{my_port}")

    my_local_ip = get_local_ip()
    if my_local_ip:
        print(f"[OK] My local address: {my_local_ip}:{LOCAL_PORT}")

    # 2. Публикуем свой адрес (внешний + локальный) — сервер будет его
    #    забирать и тоже слать punch
    print(f"[INFO] Publishing my address to ntfy ('{CLIENT_TOPIC}')...")
    if not publish_address(CLIENT_TOPIC, my_ip, my_port, my_local_ip, LOCAL_PORT):
        print("[ERROR] Failed to publish own address, aborting")
        sock.close()
        return
    print("[OK] Address published")

    # 3. Забираем адрес сервера (внешний + локальный)
    print(f"\n[INFO] Getting server address from ntfy ('{SERVER_TOPIC}')...")
    server_address, server_local_address = fetch_address(SERVER_TOPIC)

    if not server_address:
        print("[ERROR] Could not get server address after multiple attempts")
        print("[INFO] Make sure the C++ server is running and published its address")
        sock.close()
        return

    print(f"[OK] Server external address: {server_address}")
    if server_local_address:
        print(f"[OK] Server local address: {server_local_address}")

    address_to_use = server_address

    # Если внешний IP сервера совпадает с нашим — мы оба сидим за одним
    # роутером в одной сети. Punch на общий внешний IP тут не поможет:
    # это упрётся в NAT hairpinning (роутер должен завернуть пакет,
    # адресованный на собственный WAN IP, обратно в LAN — многие
    # домашние роутеры этого просто не умеют). Переключаемся на
    # локальный адрес сервера, который в одной сети виден напрямую.
    server_ext_ip = server_address.split(":")[0]
    if server_ext_ip == my_ip:
        print("\n[WARN] ============================================")
        print("[WARN] Client and server share the same external IP!")
        print("[WARN] You are both on the same local network.")
        print("[WARN] Real hole punching CANNOT be tested this way —")
        print("[WARN] most home routers don't support NAT hairpinning.")
        if server_local_address:
            print(f"[WARN] Switching to server's LOCAL address instead: {server_local_address}")
            address_to_use = server_local_address
        else:
            print("[WARN] Server did not report a local address — punch will likely fail.")
        print("[WARN] ============================================\n")

    try:
        server_ip, server_port_str = address_to_use.split(":")
        server_port = int(server_port_str)
    except Exception:
        print("[ERROR] Invalid server address format")
        sock.close()
        return

    # 4. Одновременный punch: шлём HELLO серверу в отдельном потоке,
    #    параллельно слушая ответ на этом же сокете. Сервер в это же
    #    время делает то же самое в обратную сторону.
    sock.settimeout(1.0)
    stop_event = threading.Event()

    puncher = threading.Thread(
        target=punch_loop,
        args=(sock, server_ip, server_port, stop_event),
        daemon=True,
    )
    puncher.start()

    print(f"\n[INFO] Connecting to {server_ip}:{server_port} (punching + listening)...")

    success = False
    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(1024)
            print(f"[OK] Received response from {addr}: {data.decode(errors='replace')}")
            success = True
            break
        except socket.timeout:
            continue
        except Exception as e:
            print(f"[ERROR] Receive error: {e}")
            break

    stop_event.set()

    if success:
        print("[OK] Connection successful! Hole punching worked.")
    else:
        print("[ERROR] No response received.")
        print("[INFO] Possible causes: symmetric NAT on one side, firewall blocking UDP, "
              "or the server hasn't fetched your address yet — try running the client again.")

    sock.close()


if __name__ == "__main__":
    main()
