"""Импорт реальной (обезличенной) сводки в БД как демо-данные.

Запуск на ХОСТЕ (БД опубликована на 127.0.0.1:3306):
    python3 -I import_svodka.py "/путь/к/Сводка_обезличенная.xlsx" [--keep]

Берётся лист «Аварии на сети СС» (сотовая сеть — профиль ТЗ). По умолчанию
таблицы alarms и stations очищаются и наполняются реальными данными. Справочник
станций собирается из поля «Сетевой элемент». Файл сводки — недоверенные данные,
читается openpyxl в read-only; скрипт запускать с python3 -I.
"""
import os
import re
import sys
from datetime import datetime, time, timedelta

import openpyxl
import pymysql

SHEET = "Аварии на сети СС"
DATA_START = 5           # строки данных начинаются с 6-й (0-based 5)
ZERO_DT = "0000-00-00 00:00:00"
TIME_MAX = 838 * 3600 + 59 * 60 + 59
FIELDS = ["PW", "NAPR", "TEHC", "KATEG", "NETW_TYPE", "SERV_ELEM", "NUMB_RBS",
          "EQIP_TYPE", "ALARM_TYPE", "DESCR", "BEGIN_TIME", "END_TIME", "AKB",
          "DGU", "LENGHT", "PROSTOY", "COMMON_PROSTOY", "ALARM_WHO",
          "DESC_NOTICE", "STATION_CODE", "OBJ_TYPE"]


def s(v):
    return "" if v is None else str(v).strip()


def to_secs(v):
    """Ячейку времени/длительности -> секунды (или None)."""
    if v is None:
        return None
    if isinstance(v, timedelta):
        return int(v.total_seconds())
    if isinstance(v, time):
        return v.hour * 3600 + v.minute * 60 + v.second
    if isinstance(v, datetime):
        return v.hour * 3600 + v.minute * 60 + v.second
    txt = str(v).strip()
    if txt in ("", "-"):
        return None
    m = re.match(r"(?:(\d+)\s*day[s]?,\s*)?(\d+):(\d+):(\d+)", txt)
    if m:
        d = int(m.group(1) or 0)
        return d * 86400 + int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
    return None


def hms(secs):
    if secs is None:
        return None
    secs = min(int(secs), TIME_MAX)
    return "%02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)


def fmt_len(secs):
    if secs is None:
        return None
    secs = abs(int(secs))
    return "%02d.%02d.%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)


def as_dt(v):
    if isinstance(v, datetime):
        return v
    return None


def station_code(serv):
    """'VJ_K29 — Базовая станция …' -> 'VJ_K29' (если похоже на код)."""
    if not serv:
        return None
    left = re.split(r"\s[—–-]\s", serv, 1)[0].strip()
    if 2 <= len(left) <= 40 and " " not in left and left.lower() != serv.lower():
        return left
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keep" in sys.argv
    if not args:
        print("Укажите путь к xlsx"); sys.exit(1)
    path = args[0]

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[SHEET]
    now = datetime.now().replace(microsecond=0)

    rows_out, stations = [], {}
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < DATA_START:
            continue
        row = list(row) + [None] * (19 - len(row))
        begin = as_dt(row[10])
        napr = s(row[1])
        if begin is None or not napr:
            continue                       # пропускаем пустые/итоговые строки

        end_raw = row[11]
        end = as_dt(end_raw)
        is_now = (end is None)             # «НВ» или пусто -> открыта

        numb_raw = s(row[6]).upper()
        if numb_raw in ("С", "C"):
            numb = 0
        elif numb_raw.isdigit():
            numb = int(numb_raw)
        else:
            numb = None

        kateg_raw = s(row[3])
        kateg = int(kateg_raw) if kateg_raw in ("1", "2", "3") else None

        prostoy = to_secs(row[15])
        length_sec = int(((end or now) - begin).total_seconds())
        common = None if (prostoy is None or numb is None) else prostoy * (numb if numb else 1)
        serv = s(row[5])
        code = station_code(serv)

        rec = {
            "PW": s(row[0]), "NAPR": napr, "TEHC": s(row[2]), "KATEG": kateg,
            "NETW_TYPE": s(row[4]), "SERV_ELEM": serv[:450], "NUMB_RBS": numb,
            "EQIP_TYPE": s(row[7])[:450], "ALARM_TYPE": s(row[8])[:450],
            "DESCR": s(row[9])[:450], "BEGIN_TIME": begin,
            "END_TIME": (ZERO_DT if is_now else end),
            "AKB": hms(to_secs(row[12])), "DGU": hms(to_secs(row[13])),
            "LENGHT": fmt_len(length_sec),
            # PROSTOY/COMMON — строки «чч.мм.сс» без обрезки 838 ч (MOT-004)
            "PROSTOY": fmt_len(prostoy), "COMMON_PROSTOY": fmt_len(common),
            "ALARM_WHO": s(row[17])[:450],
            "DESC_NOTICE": s(row[18])[:450], "STATION_CODE": code,
            "OBJ_TYPE": "Базовая станция",
        }
        rows_out.append(rec)
        if code and code not in stations:
            stations[code] = (code, serv[:450], napr, s(row[2]), s(row[4]), s(row[7])[:450])

    conn = pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ.get("DB_USER", "report"),
        password=os.environ.get("DB_PASSWORD", "reportpass"),
        database=os.environ.get("DB_NAME", "web_report"),
        charset="utf8mb4", autocommit=True)
    with conn.cursor() as cur:
        if not keep:
            cur.execute("DELETE FROM alarms")
            cur.execute("DELETE FROM stations")
        cols = ", ".join(FIELDS)
        ph = ", ".join(["%s"] * len(FIELDS))
        cur.executemany("INSERT INTO alarms (%s) VALUES (%s)" % (cols, ph),
                        [[r[f] for f in FIELDS] for r in rows_out])
        if stations:
            cur.executemany(
                "INSERT IGNORE INTO stations (CODE, ADDR, NAPR, TEHC, NETW_TYPE, EQIP_TYPE)"
                " VALUES (%s, %s, %s, %s, %s, %s)", list(stations.values()))
        cur.execute("SELECT COUNT(*) FROM alarms")
        na = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM stations")
        ns = cur.fetchone()[0]
    conn.close()
    print("Импортировано аварий: %d, станций: %d. Всего в БД: аварий %d, станций %d."
          % (len(rows_out), len(stations), na, ns))


if __name__ == "__main__":
    main()
