"""
Pairing autonome d'une Connect Box Aldes (HF-LPB100) sur ton WiFi local
en parlant directement le protocole Hi-Flying SmartLink V3.

Usage (PowerShell, en tant qu'utilisateur normal — pas besoin d'admin) :
    python aldes_pair.py                 (le mot de passe WiFi est demandé, sans écho)
    python aldes_pair.py <mot_de_passe>  (déconseillé : reste dans l'historique du shell)

Pré-requis CRUCIAUX :
  1. La Connect Box est en mode pairing (LED clignotante). Si pas le cas,
     fais un reset factory (long press du bouton 5-10 sec).
  2. Le PC d'où tu lances ce script doit être connecté en WiFi
     **au WiFi cible** (celui où la box doit aller).
  3. Le WiFi doit être en **2,4 GHz** (le HF-LPB100 ne supporte pas le 5 GHz).
  4. Pas de pare-feu Windows qui bloque les broadcasts UDP sortants
     (Windows Defender autorise normalement par défaut).

Le module devine le SSID en sniffant les beacons de l'AP du téléphone/PC.
On lui balance juste le password encodé via la longueur des paquets.
"""
import getpass
import os
import socket
import struct
import sys
import threading
import time

# Algorithm constants (verbatim from Hi-Flying SnifferSmartLinker.java)
HEADER_COUNT = 200          # nombre de paquets header
HEADER_DELAY = 0.010        # 10 ms entre paquets header
HEADER_CAPACITY = 76        # taille des paquets header
CONTENT_COUNT = 5           # itérations de la phase content
CONTENT_DELAY = 0.050       # 50 ms entre paquets content
CHECKSUM_PRE_DELAY = 0.100  # 100 ms avant checksum
GROUP_DELAY = 0.500         # 500 ms entre itérations content
START_MARKER = 89
END_MARKER = 86
CHAR_OFFSET = 76            # 'L'
SMARTLINK_PORT = 49999
DISCOVERY_PORT = 48899

stop_flag = threading.Event()
found_modules = {}


def get_broadcast_ip():
    """Determine the local subnet broadcast address (assumes /24).

    Override with the BROADCAST_IP env var if your LAN is not a /24
    (or use 255.255.255.255)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    finally:
        s.close()
    # Assume /24 subnet (works for 99% of home networks)
    parts = local_ip.split(".")
    bcast = os.getenv("BROADCAST_IP") or ".".join(parts[:3] + ["255"])
    return local_ip, bcast


def send_packet(sock, length, dst_ip):
    """Send a UDP packet of the given LENGTH (bytes) to broadcast."""
    payload = bytes([0x05]) * length
    try:
        sock.sendto(payload, (dst_ip, SMARTLINK_PORT))
    except OSError as e:
        # winsock might complain on size — keep going
        pass


def connect_loop(sock, dst_ip, password):
    """Run the encoding loop forever (until stop_flag set)."""
    print(f"[smartlink] connect loop started, target broadcast={dst_ip}", flush=True)
    while not stop_flag.is_set():
        # HEADER phase
        for _ in range(HEADER_COUNT):
            if stop_flag.is_set():
                return
            send_packet(sock, HEADER_CAPACITY, dst_ip)
            time.sleep(HEADER_DELAY)

        # Build content array
        content = [START_MARKER]
        for b in password:  # password is UTF-8 bytes
            content.append(b + CHAR_OFFSET)
        content.append(END_MARKER)

        # CONTENT phase: 5 iterations
        for _ in range(CONTENT_COUNT):
            if stop_flag.is_set():
                return
            for i, value in enumerate(content):
                repeat = 3 if (i == 0 or i == len(content) - 1) else 1
                for _ in range(repeat):
                    if stop_flag.is_set():
                        return
                    send_packet(sock, value, dst_ip)
                    time.sleep(CONTENT_DELAY)
                time.sleep(CONTENT_DELAY)

            time.sleep(CHECKSUM_PRE_DELAY)

            # Checksum: 3 packets of length (pwd_len + 256 + 76)
            check_len = len(password) + 256 + HEADER_CAPACITY
            for t in range(3):
                if stop_flag.is_set():
                    return
                send_packet(sock, check_len, dst_ip)
                if t < 2:
                    time.sleep(CONTENT_DELAY)

            time.sleep(GROUP_DELAY)


def find_loop(sock, dst_ip):
    """Periodically broadcast 'smartlinkfind' to find newly-paired modules."""
    print("[smartlink] find loop will start in 10 s...", flush=True)
    time.sleep(10)
    print("[smartlink] find loop starting (will run for ~5 min)...", flush=True)
    for i in range(60):  # 60 iterations × 5s = 5 min total
        if stop_flag.is_set():
            return
        try:
            sock.sendto(b"smartlinkfind", (dst_ip, DISCOVERY_PORT))
        except OSError:
            pass
        time.sleep(5)


def receive_loop(sock):
    """Wait for 'smart_config' replies indicating successful pairing."""
    sock.settimeout(0.5)
    while not stop_flag.is_set():
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            return
        text = data.decode("utf-8", errors="replace")
        if "smart_config" in text:
            mac = text.replace("smart_config", "").strip()
            ip = addr[0]
            if ip in ("0.0.0.0",) or ":" in ip:
                continue
            if mac not in found_modules:
                found_modules[mac] = ip
                print(f"\n*** PAIRED ! mac={mac}  ip={ip} ***\n", flush=True)


def main():
    if len(sys.argv) > 2:
        sys.exit(__doc__)

    password_str = sys.argv[1] if len(sys.argv) == 2 else getpass.getpass("Mot de passe WiFi : ")
    password = password_str.encode("utf-8")
    if len(password) < 1:
        sys.exit("Le mot de passe ne peut pas être vide.")

    local_ip, bcast_ip = get_broadcast_ip()
    print(f"[init] PC IP: {local_ip}, broadcast: {bcast_ip}", flush=True)
    print(f"[init] WiFi password length: {len(password)} bytes", flush=True)
    print(f"[init] HF-LPB100 will pick up SSID from your PC's current WiFi association", flush=True)
    print(f"[init] Make sure you're connected to the TARGET 2.4 GHz WiFi.", flush=True)

    # UDP socket: bind to port 49999, broadcast enabled
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", SMARTLINK_PORT))
    except OSError as e:
        sys.exit(f"Cannot bind UDP {SMARTLINK_PORT}: {e}")

    # Threads
    t_recv = threading.Thread(target=receive_loop, args=(sock,), daemon=True)
    t_recv.start()

    t_conn = threading.Thread(target=connect_loop, args=(sock, bcast_ip, password), daemon=True)
    t_conn.start()

    t_find = threading.Thread(target=find_loop, args=(sock, bcast_ip), daemon=True)
    t_find.start()

    # Wait until paired or 5 min timeout
    start = time.time()
    next_status = 10
    try:
        while time.time() - start < 300:
            if found_modules:
                # Got it — let's give it a few more secs in case multiple modules
                time.sleep(3)
                break
            time.sleep(1)
            elapsed = time.time() - start
            if elapsed >= next_status:
                print(f"[wait] {int(elapsed)} s — modules found: {len(found_modules)}", flush=True)
                next_status += 10
    except KeyboardInterrupt:
        print("\n[abort] interrupted by user")
    finally:
        stop_flag.set()
        sock.close()

    if found_modules:
        print("\n=== SUCCESS ===")
        for mac, ip in found_modules.items():
            print(f"  module mac={mac}  ip={ip}")
        print("\nLa Connect Box devrait maintenant être joignable.")
        print("Note son adresse MAC : c'est la valeur DEVICE_MAC du docker-compose.")
    else:
        print("\n=== TIMEOUT ===")
        print("Aucun module n'a répondu. Causes possibles :")
        print("  - Le PC n'est pas en WiFi 2.4 GHz")
        print("  - La box n'est pas en mode pairing (LED clignotante ?)")
        print("  - Pare-feu Windows qui bloque le broadcast UDP")
        print("  - Le réseau n'est pas en /24 (essaie BROADCAST_IP=255.255.255.255)")
        sys.exit(1)


if __name__ == "__main__":
    main()
