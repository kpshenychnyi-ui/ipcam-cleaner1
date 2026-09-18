#!/usr/bin/env python3
"""
IPCAM cleaner: удаляет с FTP снимки старше MAX_AGE_DAYS и шлёт отчёт в Telegram.

Переменные окружения:
  FTP_HOST, FTP_USER, FTP_PASS   — доступ к хранилищу (обязательно)
  FTP_PORT        — по умолчанию 21
  FTP_TLS         — auto (по умолчанию) | yes | no
  CAMERA_PREFIX   — какие папки в корне чистить, по умолчанию IPCAM_
  MAX_AGE_DAYS    — по умолчанию 300 (~10 месяцев)
  DRY_RUN         — 1 = ничего не удалять, только отчёт
  QUOTA_GB        — размер тарифа в ГБ, чтобы посчитать свободное место (необязательно)
  TG_BOT_TOKEN, TG_CHAT_ID — куда слать отчёт
"""
import csv, io, json, os, sys, time, traceback, uuid, urllib.request
from datetime import datetime, timedelta, timezone
from ftplib import FTP, FTP_TLS, error_perm

ENV = os.environ.get
HOST, USER, PASS = ENV("FTP_HOST"), ENV("FTP_USER"), ENV("FTP_PASS")
PORT = int(ENV("FTP_PORT") or 21)
TLS_MODE = (ENV("FTP_TLS") or "auto").lower()
PREFIX = ENV("CAMERA_PREFIX") or "IPCAM_"
MAX_AGE_DAYS = int(ENV("MAX_AGE_DAYS") or 300)
DRY_RUN = (ENV("DRY_RUN") or "0").lower() in ("1", "true", "yes")
QUOTA_GB = float(ENV("QUOTA_GB")) if ENV("QUOTA_GB") else None
TG_TOKEN, TG_CHAT = ENV("TG_BOT_TOKEN"), ENV("TG_CHAT_ID")

NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=MAX_AGE_DAYS)


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} Б"
        n /= 1024


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def connect():
    """FTPS, если сервер умеет; иначе обычный FTP (auto)."""
    if TLS_MODE in ("auto", "yes"):
        try:
            log(f"Подключаюсь к {HOST} по FTPS...")
            f = FTP_TLS(timeout=60)
            f.connect(HOST, PORT)
            f.login(USER, PASS)
            f.prot_p()
            f.set_pasv(True)
            return f, True
        except Exception as e:
            log(f"FTPS не удалось ({e}), пробую обычный FTP")
            if TLS_MODE == "yes":
                raise
    f = FTP(timeout=60)
    f.connect(HOST, PORT)
    f.login(USER, PASS)
    f.set_pasv(True)
    return f, False


def parse_mlsd_time(s):
    s = s.split(".")[0]
    return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def listdir(ftp, path):
    """Возвращает [(name, is_dir, size, mtime_utc)]."""
    try:
        out = []
        for name, facts in ftp.mlsd(path, facts=["type", "size", "modify"]):
            t = facts.get("type", "").lower()
            if t in ("cdir", "pdir") or name in (".", ".."):
                continue
            is_dir = t == "dir"
            size = int(facts.get("size", 0) or 0)
            mtime = parse_mlsd_time(facts["modify"]) if "modify" in facts else None
            out.append((name, is_dir, size, mtime))
        return out
    except error_perm as e:
        if not str(e).startswith("500") and not str(e).startswith("502"):
            raise
    # Фоллбэк для серверов без MLSD: NLST + MDTM + SIZE
    out = []
    for full in ftp.nlst(path):
        name = full.rsplit("/", 1)[-1]
        if name in (".", ".."):
            continue
        p = f"{path}/{name}"
        try:
            size = ftp.size(p)
            mt = ftp.sendcmd(f"MDTM {p}").split()[1]
            out.append((name, False, size or 0, parse_mlsd_time(mt)))
        except error_perm:
            out.append((name, True, 0, None))
    return out


class Stats:
    def __init__(self):
        self.del_n = self.del_b = self.keep_n = self.keep_b = 0
        self.oldest_kept = None
        self.dirs_removed = 0


def clean(ftp, path, st, deleted_rows, errors):
    """Рекурсивно чистит path. Возвращает True, если папка стала пустой."""
    entries = listdir(ftp, path)
    st.dirs_scanned = getattr(st, "dirs_scanned", 0) + 1
    log(f"{path}: {len(entries)} объектов (всего старых: {st.del_n}, свежих: {st.keep_n})")
    remaining = 0
    for name, is_dir, size, mtime in entries:
        p = f"{path}/{name}"
        if is_dir:
            if clean(ftp, p, st, deleted_rows, errors):
                try:
                    if not DRY_RUN:
                        ftp.rmd(p)
                    st.dirs_removed += 1
                    continue
                except Exception as e:
                    errors.append(f"rmdir {p}: {e}")
            remaining += 1
            continue
        if mtime is not None and mtime < CUTOFF:
            try:
                if not DRY_RUN:
                    ftp.delete(p)
                st.del_n += 1
                st.del_b += size
                deleted_rows.append((p, size, mtime.isoformat()))
                continue
            except Exception as e:
                errors.append(f"delete {p}: {e}")
        remaining += 1
        st.keep_n += 1
        st.keep_b += size
        if mtime and (st.oldest_kept is None or mtime < st.oldest_kept):
            st.oldest_kept = mtime
    return remaining == 0


def tg(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in (data or {}).items():
            body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fname, content) in files.items():
            body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                       f"Content-Type: text/csv\r\n\r\n".encode())
            body.write(content + b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, body.getvalue(),
                                     {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, json.dumps(data).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def send_report(text, csv_bytes=None):
    print(text)
    if not (TG_TOKEN and TG_CHAT):
        print("TG_BOT_TOKEN/TG_CHAT_ID не заданы — отчёт только в лог.")
        return
    tg("sendMessage", {"chat_id": TG_CHAT, "text": text[:4000], "parse_mode": "HTML"})
    if csv_bytes:
        tg("sendDocument", {"chat_id": TG_CHAT, "caption": "Полный список удалённых файлов"},
           {"document": (f"deleted_{NOW:%Y-%m-%d}.csv", csv_bytes)})


def main():
    if not (HOST and USER and PASS):
        sys.exit("Нужны FTP_HOST, FTP_USER, FTP_PASS")
    t0 = time.time()
    ftp, is_tls = connect()
    log(f"Подключено ({'FTPS' if is_tls else 'FTP'})")
    cams = sorted(n for n, is_dir, _, _ in listdir(ftp, "") or listdir(ftp, "/")
                  if is_dir and n.startswith(PREFIX))
    if not cams:
        raise RuntimeError(f"Не найдено папок с префиксом {PREFIX} — ничего не трогаю.")
    log("Камеры:", ", ".join(cams))

    per_cam, deleted_rows, errors = {}, [], []
    for cam in cams:
        st = Stats()
        try:
            clean(ftp, cam, st, deleted_rows, errors)
        except Exception as e:
            errors.append(f"{cam}: {e}")
        per_cam[cam] = st
    try:
        ftp.quit()
    except Exception:
        pass

    tot_del_n = sum(s.del_n for s in per_cam.values())
    tot_del_b = sum(s.del_b for s in per_cam.values())
    tot_keep_b = sum(s.keep_b for s in per_cam.values())
    tot_keep_n = sum(s.keep_n for s in per_cam.values())

    mode = "🧪 ТЕСТОВЫЙ ПРОГОН (ничего не удалено)" if DRY_RUN else "🧹 Очистка выполнена"
    verb = "будет удалено" if DRY_RUN else "удалено"
    lines = [f"<b>{mode}</b>",
             f"Хранилище: {HOST}{'' if is_tls else ' ⚠️ без шифрования (FTP)'}",
             f"Порог: старше {MAX_AGE_DAYS} дн. (до {CUTOFF:%d.%m.%Y})", ""]
    for cam, s in per_cam.items():
        oldest = s.oldest_kept.strftime("%d.%m.%Y") if s.oldest_kept else "—"
        lines.append(f"<b>{cam}</b>: {verb} {s.del_n} ({human(s.del_b)}), "
                     f"осталось {s.keep_n} ({human(s.keep_b)}), самый старый: {oldest}")
    lines += ["", f"<b>Итого {verb}: {tot_del_n} файлов, {human(tot_del_b)}</b>",
              f"Занято снимками после очистки: {human(tot_keep_b)} ({tot_keep_n} файлов)"]
    if QUOTA_GB:
        quota_b = QUOTA_GB * 1024 ** 3
        free = quota_b - tot_keep_b
        lines.append(f"Свободно: ~{human(free)} из {QUOTA_GB:g} ГБ ({free / quota_b * 100:.0f}%)")
    if errors:
        lines += ["", f"⚠️ Ошибок: {len(errors)}"] + [f"• {e}" for e in errors[:10]]
    lines.append(f"\nВремя: {time.time() - t0:.0f} с")

    csv_bytes = None
    if deleted_rows:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["path", "size_bytes", "modified_utc"])
        w.writerows(deleted_rows)
        csv_bytes = buf.getvalue().encode("utf-8")
    send_report("\n".join(lines), csv_bytes)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"❌ <b>IPCAM cleaner упал</b>\n<code>{type(e).__name__}: {e}</code>"
        try:
            send_report(msg)
        finally:
            traceback.print_exc()
            sys.exit(1)
