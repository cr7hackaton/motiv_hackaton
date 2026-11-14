"""Наполнение демо-данными для теста и показа.

Запуск внутри контейнера:
    docker compose exec app python seed.py          # добавить демо-аварии
    docker compose exec app python seed.py --reset   # сначала очистить аварии

Справочник БС наполняется автоматически при старте приложения (bootstrap).
Аварии генерируются синтетически: разные ТЦ, типы, категории, часть открыта (НВ),
несколько по электросети — чтобы демонстрировать фильтр «проблема» и историю БС.
"""
import random
import sys
from datetime import datetime, timedelta

from app import (FIELDS, STATIONS_SEED, ZERO_DT, common_prostoy_sec, connect,
                 duration_sec, fmt_hms, to_sql_time)

random.seed(42)

TYPES = ["РРС", "ССС", "ЭЛ", "RBS", "МСПД", "ТМ", "ОПС", "ПР", "АВР", "Аренда"]
# Чаще встречаются питание и оборудование — как в реальной эксплуатации
TYPE_WEIGHTS = [8, 3, 18, 14, 7, 6, 4, 5, 12, 3]

DESCR_BY_TYPE = {
    "ЭЛ": "Отключение внешнего электроснабжения (промсеть)",
    "АВР": "Сработал автоввод резерва, работа от АКБ/ГУ",
    "РРС": "Потеря радиорелейного пролёта",
    "RBS": "Отказ оборудования базовой станции",
    "МСПД": "Деградация сети передачи данных",
    "ТМ": "Авария на транспортной сети",
    "ОПС": "Срабатывание охранно-пожарной сигнализации",
    "ССС": "Проблема спутникового канала",
    "ПР": "Прочая неисправность",
    "Аренда": "Недоступен арендованный канал",
}

NOTIFY = ["Инженеру ТЦ по телефону", "Группе эксплуатации, Telegram",
          "Старшему смены", "Подрядчику по электроснабжению", "—"]


def rec_for(station, begin, dur_sec, atype, kateg, numb, open_now, n):
    end = None if open_now else begin + timedelta(seconds=dur_sec)
    prostoy = random.choice([5 * 60, 15 * 60, 30 * 60, 60 * 60, 90 * 60, 150 * 60])
    code, addr, napr, tehc, netw, eqip = station
    rec = {
        "PW": "https://crm.motiv/NTT-%05d" % n,
        "NAPR": napr, "TEHC": tehc, "KATEG": kateg, "NETW_TYPE": netw,
        "SERV_ELEM": addr, "NUMB_RBS": numb, "EQIP_TYPE": eqip,
        "ALARM_TYPE": atype, "DESCR": DESCR_BY_TYPE.get(atype, ""),
        "BEGIN_TIME": begin, "END_TIME": end if end else ZERO_DT,
        "AKB": to_sql_time(random.choice([None, 20 * 60, 45 * 60, 90 * 60])) if atype in ("ЭЛ", "АВР") else None,
        "DGU": to_sql_time(random.choice([None, 60 * 60, 120 * 60])) if atype in ("ЭЛ", "АВР") else None,
        "LENGHT": fmt_hms(duration_sec(begin, end)),
        "PROSTOY": to_sql_time(prostoy),
        "COMMON_PROSTOY": to_sql_time(common_prostoy_sec(prostoy, numb)),
        "ALARM_WHO": random.choice(NOTIFY),
        "DESC_NOTICE": "Демо-запись для теста интерфейса.",
        "STATION_CODE": code,
    }
    return rec


def main():
    reset = "--reset" in sys.argv
    conn = connect()
    with conn.cursor() as cur:
        if reset:
            cur.execute("DELETE FROM alarms")
            print("Аварии очищены.")
        cols = ", ".join(FIELDS)
        ph = ", ".join(["%s"] * len(FIELDS))
        sql = "INSERT INTO alarms (%s) VALUES (%s)" % (cols, ph)
        now = datetime.now().replace(microsecond=0)
        n = 1000
        made = 0
        for _ in range(36):
            n += 1
            st = random.choice(STATIONS_SEED)
            atype = random.choices(TYPES, weights=TYPE_WEIGHTS)[0]
            kateg = random.choices([1, 2, 3], weights=[2, 4, 6])[0]
            numb = random.choice([0, 1, 1, 2, 3, 5, 12, 40, 150])  # 0 => сектор «С»
            days_ago = random.randint(0, 29)
            begin = now - timedelta(days=days_ago,
                                    hours=random.randint(0, 23),
                                    minutes=random.randint(0, 59))
            dur = random.randint(10 * 60, 10 * 3600)
            open_now = (days_ago <= 2 and random.random() < 0.5)
            rec = rec_for(st, begin, dur, atype, kateg, numb, open_now, n)
            cur.execute(sql, [rec[f] for f in FIELDS])
            made += 1
        cur.execute("SELECT COUNT(*) AS n FROM alarms")
        total = cur.fetchone()["n"]
    conn.close()
    print("Добавлено демо-аварий: %d. Всего в базе: %d." % (made, total))


if __name__ == "__main__":
    main()
