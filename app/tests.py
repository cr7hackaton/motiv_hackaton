"""Юнит-тесты чистых функций (MOT-005).

Запуск (в контейнере, БД не требуется):
    docker compose exec -e SKIP_BOOTSTRAP=1 app python -m unittest tests -v
"""
import os
import unittest
from datetime import datetime

os.environ["SKIP_BOOTSTRAP"] = "1"   # не подключаться к БД при импорте
from app import (SECTOR, common_prostoy_sec, duration_sec, fmt_hms,  # noqa: E402
                 parse_dt, parse_hms, to_sql_time, form_to_record)


class TimeFuncs(unittest.TestCase):
    def test_parse_hms_ok(self):
        self.assertEqual(parse_hms("01.30.00"), 5400)
        self.assertEqual(parse_hms("00:45:10"), 2710)
        self.assertEqual(parse_hms("02.05"), 7500)      # чч.мм
        self.assertIsNone(parse_hms(""))

    def test_parse_hms_bad(self):
        for bad in ("aa.bb.cc", "1.70.00", "1.00.99", "-1.00.00"):
            with self.assertRaises(ValueError):
                parse_hms(bad)

    def test_fmt_hms(self):
        self.assertEqual(fmt_hms(5400), "01.30.00")
        self.assertEqual(fmt_hms(0), "00.00.00")
        self.assertEqual(fmt_hms(None), "")
        # больше суток и больше 838 ч не теряет часы в тексте
        self.assertEqual(fmt_hms(100 * 3600), "100.00.00")

    def test_to_sql_time_cap(self):
        # TIME ограничен 838:59:59 — большие значения обрезаются при хранении
        self.assertEqual(to_sql_time(100 * 3600), "100:00:00")
        self.assertEqual(to_sql_time(1000 * 3600), "838:59:59")
        self.assertIsNone(to_sql_time(None))

    def test_common_prostoy(self):
        self.assertEqual(common_prostoy_sec(600, 10), 6000)
        self.assertEqual(common_prostoy_sec(600, SECTOR), 600)  # сектор = 1 БС
        self.assertIsNone(common_prostoy_sec(None, 5))
        self.assertIsNone(common_prostoy_sec(600, None))

    def test_duration(self):
        b = datetime(2026, 1, 1, 10, 0, 0)
        e = datetime(2026, 1, 1, 12, 30, 0)
        self.assertEqual(duration_sec(b, e), 9000)
        self.assertIsNone(duration_sec(None, e))

    def test_parse_dt(self):
        self.assertEqual(parse_dt("2026-08-29T11:06"), datetime(2026, 8, 29, 11, 6))
        self.assertIsNone(parse_dt(""))
        with self.assertRaises(ValueError):
            parse_dt("31-13-2026")


class FormValidation(unittest.TestCase):
    def base(self, **over):
        f = {"PW": "", "NAPR": "СВН", "TEHC": "ЕКБ", "KATEG": "2",
             "NETW_TYPE": "GSM LTE", "SERV_ELEM": "x", "NUMB_RBS": "3",
             "EQIP_TYPE": "", "ALARM_TYPE": "ЭЛ", "DESCR": "",
             "BEGIN_TIME": "2026-08-29T11:06", "END_NOW": "1",
             "AKB": "", "DGU": "", "PROSTOY": "00.30.00",
             "ALARM_WHO": "", "DESC_NOTICE": "", "STATION_CODE": ""}
        f.update(over)
        return f

    def test_valid_without_pw(self):
        rec, errors = form_to_record(self.base())   # PW пуст — ок (MOT-020)
        self.assertEqual(errors, [])
        self.assertEqual(rec["KATEG"], 2)
        self.assertEqual(rec["COMMON_PROSTOY"], "01.30.00")  # 30мин*3БС, строка чч.мм.сс

    def test_reject_bad_choice(self):
        _, e = form_to_record(self.base(NAPR="ВЗЛОМ"))
        self.assertTrue(any("Направление" in x for x in e))

    def test_reject_bad_category(self):
        _, e = form_to_record(self.base(KATEG="9"))
        self.assertTrue(any("Категория" in x for x in e))

    def test_sector(self):
        rec, e = form_to_record(self.base(NUMB_RBS="С"))
        self.assertEqual(rec["NUMB_RBS"], SECTOR)
        self.assertEqual(e, [])

    def test_bad_numb(self):
        _, e = form_to_record(self.base(NUMB_RBS="99999"))
        self.assertTrue(any("Количество БС" in x for x in e))

    def test_long_duration_not_capped(self):
        # MOT-004: простой > 838 ч не обрезается (хранится строкой)
        rec, e = form_to_record(self.base(PROSTOY="500.00.00", NUMB_RBS="3"))
        self.assertEqual(e, [])
        self.assertEqual(rec["PROSTOY"], "500.00.00")
        self.assertEqual(rec["COMMON_PROSTOY"], "1500.00.00")   # 500ч * 3 БС


if __name__ == "__main__":
    unittest.main(verbosity=2)
