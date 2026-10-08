"""Многопоточная загрузка файла по HTTP Range с докачкой.

Сервер ограничивает скорость на одно соединение, поэтому файл режется на куски,
которые качаются параллельно. Готовые куски запоминаются в <файл>.parts, и при
перезапуске загрузка продолжается с места остановки.

    python pget.py URL ВЫХОДНОЙ_ФАЙЛ [ПОТОКОВ] [РАЗМЕР_КУСКА_МБ]
"""
import json, os, sys, threading, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0"}


def resolve(url):
    """Идём по редиректам до конечного URL и узнаём размер файла."""
    req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers["Content-Range"].split("/")[1])
        return r.geturl(), total


def main():
    url, out = sys.argv[1], sys.argv[2]
    threads = int(sys.argv[3]) if len(sys.argv) > 3 else 16
    piece = (int(sys.argv[4]) if len(sys.argv) > 4 else 16) * 1024 * 1024

    final_url, total = resolve(url)
    state_path = out + ".parts"
    done = set(json.load(open(state_path))) if os.path.exists(state_path) else set()
    if not os.path.exists(out):
        with open(out, "wb") as f:
            f.truncate(total)

    pieces = [i for i in range((total + piece - 1) // piece) if i not in done]
    lock = threading.Lock()
    state = {"url": final_url, "bytes": 0, "t0": time.time()}

    def fetch(i):
        start = i * piece
        end = min(start + piece, total) - 1
        for attempt in range(30):
            try:
                req = urllib.request.Request(state["url"], headers={**UA, "Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(req, timeout=60) as r, open(out, "r+b") as f:
                    f.seek(start)
                    got = 0
                    while chunk := r.read(1 << 16):
                        f.write(chunk)
                        got += len(chunk)
                        with lock:
                            state["bytes"] += len(chunk)
                if got != end - start + 1:
                    raise IOError(f"кусок {i}: получено {got} из {end - start + 1}")
                with lock:
                    done.add(i)
                    json.dump(sorted(done), open(state_path, "w"))
                return
            except Exception as e:
                # подписанные ссылки CDN истекают — переразрешаем URL
                if "403" in str(e) or "410" in str(e):
                    try:
                        state["url"], _ = resolve(url)
                    except Exception:
                        pass
                time.sleep(min(2 * (attempt + 1), 20))
        raise RuntimeError(f"кусок {i} не скачан")

    queue = list(pieces)
    qlock = threading.Lock()

    def worker():
        while True:
            with qlock:
                if not queue:
                    return
                i = queue.pop(0)
            fetch(i)

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for w in workers:
        w.start()
    already = len(done) * piece
    while any(w.is_alive() for w in workers):
        time.sleep(10)
        el = time.time() - state["t0"]
        have = min(already + state["bytes"], total)
        speed = state["bytes"] / el if el else 0
        eta = (total - have) / speed if speed else 0
        print(f"{os.path.basename(out)}: {have / 1e9:.2f}/{total / 1e9:.2f} ГБ, "
              f"{speed / 1e6:.1f} МБ/с, осталось ~{eta / 60:.0f} мин", flush=True)

    if len(done) * piece >= total or len(done) == (total + piece - 1) // piece:
        os.remove(state_path)
        print(f"ГОТОВО {out} ({total} байт)", flush=True)
    else:
        sys.exit("не все куски скачаны — перезапустите для докачки")


if __name__ == "__main__":
    main()
