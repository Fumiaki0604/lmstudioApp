"""自宅Wi-Fi在室検知の雛形。

iPhoneが自宅LANに接続しているか(=在室)を、MACアドレスがARPキャッシュに
現れるかで判定する。IPアドレスではなくMACアドレスで見るため、DHCPで
IPが変わってもルーター側の設定(固定IP化)は不要。

ARPキャッシュはデバイスが実際に離脱した後もしばらく古いエントリが
残ることがあるため、該当エントリを見つけたらそのIPに直接pingを打ち、
今まさに応答があるかを確認してから在室と判定する。

使い方:
    python presence.py            # 1回だけ状態を表示
    python presence.py --watch    # ポーリングし続け、状態が変わったら表示
"""
import argparse
import re
import subprocess
import time

PHONE_MAC = "fc:a5:c8:df:96:07"
SUBNET_PREFIX = "192.168.0"  # 自宅LANのサブネット
PING_TIMEOUT_MS = 200  # サブネット全体のARPキャッシュ更新用(並列実行なので短くて良い)
CONFIRM_PING_TIMEOUT_MS = 1000  # 対象1台への疎通確認用(初回pingは遅くなりがちなので長めに)
POLL_INTERVAL_SEC = 30


def _normalize_mac(mac: str) -> str:
    return ":".join(f"{int(octet, 16):02x}" for octet in mac.split(":"))


def _refresh_arp_cache(subnet_prefix: str = SUBNET_PREFIX, timeout_ms: int = PING_TIMEOUT_MS) -> None:
    procs = [
        subprocess.Popen(
            ["ping", "-c", "1", "-W", str(timeout_ms), f"{subnet_prefix}.{i}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for i in range(1, 255)
    ]
    for p in procs:
        p.wait()


def _ping(ip: str, timeout_ms: int = CONFIRM_PING_TIMEOUT_MS) -> bool:
    result = subprocess.run(
        ["ping", "-c", "1", "-W", str(timeout_ms), ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def is_home(mac: str = PHONE_MAC, refresh: bool = True) -> bool:
    if refresh:
        _refresh_arp_cache()

    target = _normalize_mac(mac)
    result = subprocess.run(["arp", "-a"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        m = re.search(r"\(([\d.]+)\) at ([0-9a-fA-F:]+)", line)
        if m and _normalize_mac(m.group(2)) == target:
            ip = m.group(1)
            return _ping(ip)  # ARPキャッシュが古い可能性があるので実際に疎通確認する
    return False


def watch(mac: str = PHONE_MAC, interval_sec: int = POLL_INTERVAL_SEC) -> None:
    last_state = None
    while True:
        state = is_home(mac)
        if state != last_state:
            label = "在室" if state else "不在"
            print(f"[{time.strftime('%H:%M:%S')}] {label}")
            last_state = state
        time.sleep(interval_sec)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()

    if args.watch:
        watch()
    else:
        print("在室" if is_home() else "不在")


if __name__ == "__main__":
    main()
