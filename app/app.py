"""Веб отчетность — учет аварий на базовых станциях сотовой сети.

Flask + PyMySQL, СУБД MariaDB 10.1. Роли: admin / duty / engineer.
"""
import csv
import io
import os
import time
from collections import Counter
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlencode

import pymysql
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
# Безопасные флаги cookie (MOT-053). SECURE включаем только за HTTPS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    MAX_CONTENT_LENGTH=4 * 1024 * 1024,
)

# --- Справочники (фиксированные значения из ТЗ) -----------------------------
NAPR_VALUES = ["ХМЯН", "СВН", "ЮЗН", "Вся сеть"]
TC_VALUES = ["НБР", "АСБ", "ИРБ", "КРТ", "НТГ", "НТР", "НУРЛ", "НВРТ", "НУРГ",
             "ОКТ", "СЛХ", "СОВ", "СРГ", "ХМН", "ЕКБ", "КМУ", "КРГ", "НСР",
             "ШДР"]  # ШДР встречается в реальной сводке (MOT-021)
KATEG_VALUES = [1, 2, 3]
# Тип сети: ТЗ + реальный формат сводки («GSM LTE»)
NETW_VALUES = ["GSM LTE", "GSM", "LTE", "LTE/GSM"]
# Типы аварий: ТЗ + реально встречающиеся в сводке сотовой сети (РРЛ, ДГУ, ИБП, ВОЛС)
ALARM_TYPE_VALUES = ["ЭЛ", "RBS", "РРЛ", "РРС", "ВОЛС", "ТМ", "МСПД", "ОПС",
                     "ДГУ", "ИБП", "АВР", "ССС", "ПР", "Аренда"]

# Тип объекта аварии: обычно станция, но может быть и вышестоящий элемент
# (например, контроллер, за которым отключается много БС). «Количество БС» тогда —
# сколько станций затронуто этим объектом.
OBJ_TYPE_VALUES = ["Базовая станция", "Контроллер (BSC/RNC)", "Узел / коммутатор",
                   "Транспорт / ВОЛС", "Электропитание / подстанция", "Другое"]

# Подсказки к типам (расшифровки по справочнику сводки/практике ЦОУ).
ALARM_TYPE_HINTS = {
    "ЭЛ": "Электросеть / отключение электропитания",
    "RBS": "Оборудование базовой станции",
    "РРЛ": "Радиорелейная линия", "РРС": "Радиорелейная сеть связи",
    "ВОЛС": "Волоконно-оптическая линия связи", "ТМ": "Телеметрия / транспорт",
    "МСПД": "Сеть передачи данных", "ОПС": "Охранно-пожарная сигнализация",
    "ДГУ": "Дизель-генераторная установка", "ИБП": "Источник бесперебойного питания",
    "АВР": "Автоввод резерва (питание)", "ССС": "Спутниковая связь",
    "ПР": "Прочее", "Аренда": "Арендованный канал/объект",
}

# Группы «проблем» для быстрой фильтрации (несколько типов одним кликом).
PROBLEM_GROUPS = {
    "power": ("Электросеть / питание", ["ЭЛ", "АВР", "ДГУ", "ИБП"]),
    "rrl": ("Радиорелейная (РРЛ/РРС)", ["РРЛ", "РРС"]),
    "transport": ("Транспорт / СПД / ВОЛС", ["ТМ", "МСПД", "ВОЛС"]),
    "rbs": ("Оборудование БС", ["RBS"]),
    "alarm": ("ОПС (сигнализация)", ["ОПС"]),
}

ROLES = {"owner": "Владелец", "admin": "Администратор",
         "duty": "Дежурный", "engineer": "Инженер"}
# Роль «Владелец» — единственная и не назначается через интерфейс (выдаётся один раз).

# Матрица прав. Единственный источник истины для проверок доступа.
# Инженер — только просмотр; дежурный — работа с авариями (в т.ч. удаление);
# администратор — как дежурный + управление пользователями (дежурные/инженеры);
# владелец — всё, включая управление администраторами.
PERMISSIONS = {
    "alarm_create": {"owner", "admin", "duty"},
    "alarm_edit": {"owner", "admin", "duty"},
    "alarm_delete": {"owner", "admin", "duty"},
    "report_view": {"owner", "admin", "duty", "engineer"},
    "report_export": {"owner", "admin", "duty", "engineer"},
    "user_view": {"owner", "admin"},     # видеть список сотрудников
    "user_manage": {"owner", "admin"},   # управлять пользователями (с ограничением по уровню)
    "audit_view": {"owner", "admin"},
}

# Какие роли может НАЗНАЧАТЬ пользователь и кем может управлять — зависит от его уровня.
# Владелец: админы/дежурные/инженеры. Администратор: только дежурные/инженеры.
ROLE_SCOPE = {
    "owner": ["admin", "duty", "engineer"],
    "admin": ["duty", "engineer"],
}

SECTOR = 0          # NUMB_RBS = 0 означает «С» (сектор)
ZERO_DT = "0000-00-00 00:00:00"   # «НВ» — настоящее время
TIME_MAX_SEC = 838 * 3600 + 59 * 60 + 59   # предел типа TIME в MariaDB

# Колонки ТЗ + служебные STATION_CODE и OBJ_TYPE (nullable, расширения).
FIELDS = ["PW", "NAPR", "TEHC", "KATEG", "NETW_TYPE", "SERV_ELEM", "NUMB_RBS",
          "EQIP_TYPE", "ALARM_TYPE", "DESCR", "BEGIN_TIME", "END_TIME", "AKB",
          "DGU", "LENGHT", "PROSTOY", "COMMON_PROSTOY", "ALARM_WHO",
          "DESC_NOTICE", "STATION_CODE", "OBJ_TYPE"]

CSV_HEADERS = ["ID", "Номер PW/NTT", "Направление", "ТЦ", "Категория",
               "Тип сети", "Сетевой элемент, услуга", "Количество БС",
               "Тип оборудования", "Тип", "Описание", "Начало", "Окончание",
               "АКБ", "ГУ", "Длительность аварии", "Время перерыва связи",
               "Общее время простоя БС", "Уведомление об аварии (Кому)",
               "Подробное описание", "Код БС (справочник)", "Тип объекта"]

# Справочник базовых станций (демо-данные, синтетические — для автозаполнения,
# истории станции и наполнения демо). Структура: код, адрес, направление, ТЦ,
# тип сети, тип оборудования.
STATIONS_SEED = [
    ("EKB-001", "Екатеринбург, ул. Малышева, 51", "ЮЗН", "ЕКБ", "LTE/GSM", "Huawei BTS3900"),
    ("EKB-014", "Екатеринбург, ул. Щербакова, 4", "ЮЗН", "ЕКБ", "LTE", "Huawei BTS5900"),
    ("EKB-027", "Екатеринбург, пр. Космонавтов, 108", "ЮЗН", "ЕКБ", "LTE/GSM", "Ericsson RBS6201"),
    ("KMU-002", "Каменск-Уральский, ул. Ленина, 12", "ЮЗН", "КМУ", "GSM", "Ericsson RBS2206"),
    ("KRG-003", "Курган, ул. Гоголя, 30", "ЮЗН", "КРГ", "LTE/GSM", "Huawei BTS3900"),
    ("KRG-018", "Курган, ул. Карла Маркса, 70", "ЮЗН", "КРГ", "LTE", "Nokia Flexi"),
    ("NTG-004", "Нижний Тагил, пр. Ленина, 40", "СВН", "НТГ", "LTE/GSM", "Huawei BTS3900"),
    ("NTG-022", "Нижний Тагил, ул. Фрунзе, 17", "СВН", "НТГ", "GSM", "Ericsson RBS2216"),
    ("IRB-006", "Ирбит, ул. Советская, 25", "СВН", "ИРБ", "GSM", "ZTE BS8900"),
    ("SRG-007", "Сургут, пр. Ленина, 43", "ХМЯН", "СРГ", "LTE/GSM", "Huawei BTS5900"),
    ("SRG-031", "Сургут, ул. Университетская, 9", "ХМЯН", "СРГ", "LTE", "Huawei BTS5900"),
    ("NVRT-008", "Нижневартовск, ул. Мира, 60", "ХМЯН", "НВРТ", "LTE/GSM", "Nokia Flexi"),
    ("HMN-009", "Ханты-Мансийск, ул. Гагарина, 142", "ХМЯН", "ХМН", "LTE/GSM", "Ericsson RBS6601"),
    ("NURL-010", "Новый Уренгой, ул. Губкина, 15", "ХМЯН", "НУРЛ", "LTE", "Huawei BTS5900"),
    ("SLH-011", "Салехард, ул. Республики, 72", "ХМЯН", "СЛХ", "GSM", "ZTE BS8700"),
    ("SOV-012", "Советский, ул. Ленина, 20", "ХМЯН", "СОВ", "LTE/GSM", "Nokia Flexi"),
]


# --- БД ----------------------------------------------------------------------
def connect():
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ.get("DB_USER", "report"),
        password=os.environ.get("DB_PASSWORD", "reportpass"),
        database=os.environ.get("DB_NAME", "web_report"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql, args=None, one=False):
    with db().cursor() as cur:
        cur.execute(sql, args if args is not None else ())
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=None):
    with db().cursor() as cur:
        cur.execute(sql, args if args is not None else ())
        return cur.lastrowid


def bootstrap():
    """Ждём БД, самостоятельно донастраиваем схему и наполняем справочники."""
    for _ in range(60):
        try:
            conn = connect()
            break
        except pymysql.err.OperationalError:
            time.sleep(2)
    else:
        raise RuntimeError("БД недоступна")
    with conn.cursor() as cur:
        # Справочник БС и связь аварии со станцией — на случай старой БД.
        cur.execute("""CREATE TABLE IF NOT EXISTS stations (
            ID INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            CODE VARCHAR(64) NOT NULL UNIQUE,
            ADDR VARCHAR(450), NAPR VARCHAR(450), TEHC VARCHAR(450),
            NETW_TYPE VARCHAR(450), EQIP_TYPE VARCHAR(450)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("ALTER TABLE alarms ADD COLUMN IF NOT EXISTS STATION_CODE VARCHAR(64) NULL")
        cur.execute("ALTER TABLE alarms ADD COLUMN IF NOT EXISTS OBJ_TYPE VARCHAR(64) NULL")
        # PROSTOY/COMMON_PROSTOY: TIME -> VARCHAR, чтобы вмещать длительности > 838 ч (MOT-004)
        cur.execute("SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS"
                    " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='alarms'"
                    " AND COLUMN_NAME IN ('PROSTOY','COMMON_PROSTOY') AND DATA_TYPE='time'")
        if cur.fetchall():
            cur.execute("ALTER TABLE alarms MODIFY PROSTOY VARCHAR(450),"
                        " MODIFY COMMON_PROSTOY VARCHAR(450)")
        cur.execute("SELECT COUNT(*) AS n FROM stations")
        if cur.fetchone()["n"] == 0:
            cur.executemany(
                "INSERT IGNORE INTO stations (CODE, ADDR, NAPR, TEHC, NETW_TYPE, EQIP_TYPE)"
                " VALUES (%s, %s, %s, %s, %s, %s)", STATIONS_SEED)
        cur.execute("SELECT COUNT(*) AS n FROM users")
        if cur.fetchone()["n"] == 0:
            cur.execute(  # первый аккаунт — Владелец (единственный)
                "INSERT IGNORE INTO users (LOGIN, FULL_NAME, PASSWORD_HASH, ROLE, CREATED_AT)"
                " VALUES (%s, %s, %s, 'owner', NOW())",
                (os.environ.get("ADMIN_LOGIN", "admin"), "Админ Админов Админович",
                 generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin"))))
    conn.close()


def audit(action, alarm_id=None, details=""):
    execute("INSERT INTO audit_log (TS, USER_LOGIN, ACTION, ALARM_ID, DETAILS)"
            " VALUES (NOW(), %s, %s, %s, %s)",
            (session.get("login"), action, alarm_id, details[:1000]))


# --- Время -------------------------------------------------------------------
def parse_hms(value):
    """'чч.мм.сс' / 'чч:мм:сс' / 'чч.мм' -> секунды. Пусто -> None."""
    value = (value or "").strip()
    if not value:
        return None
    parts = value.replace(":", ".").split(".")
    if len(parts) == 2:
        parts.append("0")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError("Время должно быть в формате чч.мм.сс: " + value)
    h, m, s = (int(p) for p in parts)
    if m > 59 or s > 59:
        raise ValueError("Минуты и секунды должны быть 0–59: " + value)
    return h * 3600 + m * 60 + s


def fmt_hms(seconds, sep="."):
    if seconds is None:
        return ""
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return "%s%02d%s%02d%s%02d" % (sign, seconds // 3600, sep,
                                   seconds % 3600 // 60, sep, seconds % 60)


def to_sql_time(seconds):
    if seconds is None:
        return None
    return fmt_hms(min(seconds, TIME_MAX_SEC), ":")


def parse_dt(value):
    value = (value or "").strip()
    if not value:
        return None
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, f)
        except ValueError:
            pass
    raise ValueError("Неверная дата: " + value)


def duration_sec(begin, end):
    """Длительность аварии: (НВ ? сейчас : окончание) − начало.
    Не может быть отрицательной (начало в будущем / окончание раньше начала)."""
    if begin is None:
        return None
    return max(0, int(((end or datetime.now()) - begin).total_seconds()))


def common_prostoy_sec(prostoy, numb):
    if prostoy is None or numb is None:
        return None
    return prostoy * (1 if numb == SECTOR else numb)


# --- Чтение строк ------------------------------------------------------------
SELECT_ALARM = """
SELECT ID, PW, NAPR, TEHC, KATEG, NETW_TYPE, SERV_ELEM, NUMB_RBS, EQIP_TYPE,
       ALARM_TYPE, DESCR,
       DATE_FORMAT(BEGIN_TIME, '%%Y-%%m-%%d %%H:%%i:%%s') AS BEGIN_S,
       CAST(END_TIME AS CHAR) AS END_S,
       TIME_TO_SEC(AKB) AS AKB_S, TIME_TO_SEC(DGU) AS DGU_S, LENGHT,
       PROSTOY, COMMON_PROSTOY,
       ALARM_WHO, DESC_NOTICE, STATION_CODE, OBJ_TYPE
FROM alarms
"""


def parse_dur(value):
    """Строка 'чч.мм.сс' (часы без ограничения) -> секунды. Пусто/мусор -> None."""
    try:
        return parse_hms(value)
    except (ValueError, TypeError):
        return None


def decorate(row):
    """Добавляет к строке вычисляемые и отформатированные поля."""
    begin = parse_dt(row["BEGIN_S"]) if row["BEGIN_S"] else None
    end_s = row["END_S"]
    is_now = (not end_s) or end_s.startswith("0000-00-00")
    end = None if is_now else parse_dt(end_s)
    row["is_now"] = is_now
    row["begin"] = begin
    row["end"] = end
    row["begin_txt"] = begin.strftime("%d.%m.%Y %H:%M:%S") if begin else ""
    row["end_txt"] = "НВ" if is_now else end.strftime("%d.%m.%Y %H:%M:%S")
    row["begin_input"] = begin.strftime("%Y-%m-%dT%H:%M:%S") if begin else ""
    row["end_input"] = "" if is_now else end.strftime("%Y-%m-%dT%H:%M:%S")
    # Для открытых аварий длительность пересчитывается на текущий момент
    row["length_txt"] = fmt_hms(duration_sec(begin, end)) if is_now else (row["LENGHT"] or "")
    row["numb_txt"] = "С" if row["NUMB_RBS"] == SECTOR else (
        "" if row["NUMB_RBS"] is None else str(row["NUMB_RBS"]))
    row["akb_txt"] = fmt_hms(row["AKB_S"])
    row["dgu_txt"] = fmt_hms(row["DGU_S"])
    # PROSTOY/COMMON_PROSTOY хранятся строкой «чч.мм.сс» без ограничения 838 ч
    row["PROSTOY_S"] = parse_dur(row.get("PROSTOY"))
    row["prostoy_txt"] = fmt_hms(row["PROSTOY_S"])
    row["common_txt"] = fmt_hms(common_prostoy_sec(row["PROSTOY_S"], row["NUMB_RBS"]))
    return row


# --- Валидация формы ---------------------------------------------------------
def form_to_record(form):
    errors = []

    def pick(name, allowed, label, cast=str):
        v = (form.get(name) or "").strip()
        if v == "":
            errors.append("Не выбрано поле «%s»" % label)
            return None
        try:
            v = cast(v)
        except ValueError:
            v = None
        if v not in allowed:
            errors.append("Недопустимое значение поля «%s»" % label)
            return None
        return v

    rec = {
        "PW": form.get("PW", "").strip(),
        "NAPR": pick("NAPR", NAPR_VALUES, "Направление"),
        "TEHC": pick("TEHC", TC_VALUES, "ТЦ"),
        "KATEG": pick("KATEG", KATEG_VALUES, "Категория", int),
        "NETW_TYPE": pick("NETW_TYPE", NETW_VALUES, "Тип сети"),
        "SERV_ELEM": form.get("SERV_ELEM", "").strip(),
        "EQIP_TYPE": form.get("EQIP_TYPE", "").strip(),
        "ALARM_TYPE": pick("ALARM_TYPE", ALARM_TYPE_VALUES, "Тип"),
        "DESCR": form.get("DESCR", "").strip(),
        "ALARM_WHO": form.get("ALARM_WHO", "").strip(),
        "DESC_NOTICE": form.get("DESC_NOTICE", "").strip(),
        "STATION_CODE": (form.get("STATION_CODE", "").strip() or None),
        "OBJ_TYPE": (form.get("OBJ_TYPE", "").strip() or "Базовая станция"),
    }
    if rec["OBJ_TYPE"] not in OBJ_TYPE_VALUES:
        rec["OBJ_TYPE"] = "Базовая станция"
    # PW/NTT необязателен: в реальной сводке ~2/3 записей без ссылки CRM (MOT-020)
    if rec["STATION_CODE"] and len(rec["STATION_CODE"]) > 64:
        rec["STATION_CODE"] = rec["STATION_CODE"][:64]

    numb = (form.get("NUMB_RBS") or "").strip().upper()
    if numb in ("С", "C"):          # кириллическая и латинская «С»
        rec["NUMB_RBS"] = SECTOR
    elif numb.isdigit() and 1 <= int(numb) <= 10000:
        rec["NUMB_RBS"] = int(numb)
    else:
        rec["NUMB_RBS"] = None
        errors.append("Количество БС — число от 1 до 10000 либо «С» (сектор)")

    try:
        begin = parse_dt(form.get("BEGIN_TIME"))
        if begin is None:
            errors.append("Не указано начало аварии")
        end_now = form.get("END_NOW") == "1"
        end = None if end_now else parse_dt(form.get("END_TIME"))
        if not end_now and end is None:
            errors.append("Укажите окончание аварии или отметьте «НВ»")
        if begin and begin > datetime.now():
            errors.append("Начало аварии не может быть в будущем")
        if begin and end and end < begin:
            errors.append("Окончание раньше начала")
    except ValueError as e:
        errors.append(str(e))
        begin = end = None

    secs = {}
    for name, label in (("AKB", "АКБ"), ("DGU", "ГУ"),
                        ("PROSTOY", "Время перерыва связи")):
        try:
            secs[name] = parse_hms(form.get(name))
        except ValueError as e:
            secs[name] = None
            errors.append("%s: %s" % (label, e))

    rec["BEGIN_TIME"] = begin
    rec["END_TIME"] = end if end else ZERO_DT
    # АКБ/ГУ остаются типом TIME (небольшие значения); PROSTOY/COMMON — строки
    rec["AKB"] = to_sql_time(secs["AKB"])
    rec["DGU"] = to_sql_time(secs["DGU"])
    rec["PROSTOY"] = fmt_hms(secs["PROSTOY"]) if secs["PROSTOY"] is not None else None
    rec["LENGHT"] = fmt_hms(duration_sec(begin, end)) if begin else None
    common = common_prostoy_sec(secs["PROSTOY"], rec["NUMB_RBS"])
    rec["COMMON_PROSTOY"] = fmt_hms(common) if common is not None else None

    for k in ("PW", "SERV_ELEM", "EQIP_TYPE", "DESCR", "ALARM_WHO", "DESC_NOTICE"):
        if len(rec[k]) > 450:
            errors.append("Поле %s длиннее 450 символов" % k)
    return rec, errors


# --- Авторизация -------------------------------------------------------------
def safe_back(url, default):
    """Возврат только на локальные адреса (защита от open redirect)."""
    url = url or ""
    return url if url.startswith("/") and not url.startswith("//") else default


def can(perm):
    return session.get("role") in PERMISSIONS.get(perm, set())


def assignable_roles():
    """Роли, которые текущий пользователь вправе назначать."""
    return ROLE_SCOPE.get(session.get("role"), [])


def can_manage_user(target_role):
    """Может ли текущий пользователь управлять учёткой с ролью target_role
    (менять роль, блокировать, менять пароль). Владелец неприкосновенен."""
    return target_role in ROLE_SCOPE.get(session.get("role"), [])


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = os.urandom(16).hex()
    return session["csrf"]


@app.before_request
def check_csrf():
    # CSRF-токен проверяется на всех POST, включая вход (MOT-045).
    # Токен обязан существовать в сессии и совпадать — иначе пустая сессия
    # пропустила бы POST без токена (None == None).
    if request.method == "POST":
        token = session.get("csrf")
        if not token or request.form.get("csrf") != token:
            abort(403)


app.jinja_env.globals.update(can=can, ROLES=ROLES, assignable_roles=assignable_roles,
                             can_manage_user=can_manage_user, csrf_token=csrf_token,
                             safe_back=safe_back, ATYPE_HINTS=ALARM_TYPE_HINTS)


@app.context_processor
def inject_now():
    d = datetime.now()
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    mo = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
    return {"now_human": "%s, %d %s %d г." % (wd[d.weekday()], d.day, mo[d.month - 1], d.year)}


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("uid"):
            return redirect(url_for("login", next=request.path))
        user = query("SELECT ROLE, IS_ACTIVE, FULL_NAME, LOGIN FROM users WHERE ID=%s",
                     (session["uid"],), one=True)
        if not user or not user["IS_ACTIVE"]:
            session.clear()
            return redirect(url_for("login"))
        session["role"] = user["ROLE"]   # роль/имя могли измениться админом
        session["name"] = user["FULL_NAME"] or user["LOGIN"]
        return view(*a, **kw)
    return wrapped


def require(perm):
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(*a, **kw):
            if not can(perm):
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = query("SELECT * FROM users WHERE LOGIN=%s",
                     (request.form.get("login", "").strip(),), one=True)
        if (user and user["IS_ACTIVE"]
                and check_password_hash(user["PASSWORD_HASH"], request.form.get("password", ""))):
            session.clear()
            session.update(uid=user["ID"], login=user["LOGIN"], role=user["ROLE"],
                           name=user["FULL_NAME"] or user["LOGIN"])
            audit("login")
            return redirect(safe_back(request.args.get("next"), url_for("index")))
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return redirect(url_for("alarms" if can("alarm_create") else "reports"))


# --- Справочник БС: автодополнение ------------------------------------------
@app.route("/api/stations")
@login_required
def api_stations():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    like = "%" + q + "%"
    rows = query(
        "SELECT CODE, ADDR, NAPR, TEHC, NETW_TYPE, EQIP_TYPE FROM stations"
        " WHERE CODE LIKE %s OR ADDR LIKE %s ORDER BY CODE LIMIT 12", (like, like))
    return jsonify(rows)


# --- Вкладка «Аварии» --------------------------------------------------------
def choices():
    return dict(NAPR=NAPR_VALUES, TC=TC_VALUES, KATEG=KATEG_VALUES,
                NETW=NETW_VALUES, ATYPE=ALARM_TYPE_VALUES, OBJ=OBJ_TYPE_VALUES)


def prefill_from(row):
    """Словарь для формы из строки аварии (для редактирования/дублирования)."""
    return {
        "PW": row["PW"], "NAPR": row["NAPR"], "TEHC": row["TEHC"],
        "KATEG": str(row["KATEG"] or ""), "NETW_TYPE": row["NETW_TYPE"],
        "SERV_ELEM": row["SERV_ELEM"], "NUMB_RBS": row["numb_txt"],
        "EQIP_TYPE": row["EQIP_TYPE"], "ALARM_TYPE": row["ALARM_TYPE"],
        "DESCR": row["DESCR"], "BEGIN_TIME": row["begin_input"],
        "END_TIME": row["end_input"], "END_NOW": "1" if row["is_now"] else "",
        "AKB": row["akb_txt"], "DGU": row["dgu_txt"],
        "PROSTOY": row["prostoy_txt"], "ALARM_WHO": row["ALARM_WHO"],
        "DESC_NOTICE": row["DESC_NOTICE"], "STATION_CODE": row["STATION_CODE"] or "",
        "OBJ_TYPE": row["OBJ_TYPE"] or "Базовая станция",
    }


@app.route("/alarms", methods=["GET", "POST"])
@require("alarm_create")
def alarms():
    form = {}
    if request.method == "POST":
        rec, errors = form_to_record(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            form = request.form
        else:
            cols = ", ".join(FIELDS)
            ph = ", ".join(["%s"] * len(FIELDS))
            new_id = execute("INSERT INTO alarms (%s) VALUES (%s)" % (cols, ph),
                             [rec[f] for f in FIELDS])
            audit("create", new_id, rec["PW"])
            flash("Авария №%s сохранена" % new_id, "ok")
            return redirect(url_for("alarms"))
    else:
        # Быстрый ввод: дублирование последней / конкретной аварии (без PW и времени)
        dup = request.args.get("dup")
        src = None
        if dup == "last":
            src = query(SELECT_ALARM + " ORDER BY ID DESC LIMIT 1", one=True)
        elif dup and dup.isdigit():
            src = query(SELECT_ALARM + " WHERE ID=%s", (int(dup),), one=True)
        if src:
            form = prefill_from(decorate(src))
            form["PW"] = ""            # новая авария — свой номер
            form["BEGIN_TIME"] = ""    # время проставит JS («сейчас»)
            form["END_TIME"] = ""
            form["END_NOW"] = "1"
            flash("Поля заполнены из аварии №%s — проверьте и укажите номер PW/NTT" % src["ID"], "ok")
    open_rows = [decorate(r) for r in query(
        SELECT_ALARM + " WHERE END_TIME = '0000-00-00 00:00:00' OR END_TIME IS NULL"
        " ORDER BY BEGIN_TIME DESC LIMIT 200")]
    return render_template("alarms.html", c=choices(), form=form, rows=open_rows)


@app.route("/alarms/bulk", methods=["GET", "POST"])
@require("alarm_create")
def alarms_bulk():
    """Групповое заведение: одна авария сразу на много станций (например,
    отключение электросети в районе/ТЦ). Атрибуты станции берутся из справочника."""
    f = {k: (request.values.get(k, "") or "").strip() for k in ("f_napr", "f_tehc", "f_q")}
    conds, args = [], []
    if f["f_napr"] in NAPR_VALUES:
        conds.append("NAPR=%s"); args.append(f["f_napr"])
    if f["f_tehc"] in TC_VALUES:
        conds.append("TEHC=%s"); args.append(f["f_tehc"])
    if f["f_q"]:
        conds.append("(CODE LIKE %s OR ADDR LIKE %s)"); args += ["%" + f["f_q"] + "%"] * 2
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    stations_list = query("SELECT CODE, ADDR, NAPR, TEHC, NETW_TYPE, EQIP_TYPE FROM stations"
                          + where + " ORDER BY TEHC, CODE", args)

    form = {}
    if request.method == "POST" and request.form.get("do") == "create":
        form = request.form
        codes = request.form.getlist("codes")
        errors = []
        try:
            kateg_v = int(request.form.get("KATEG", ""))
        except ValueError:
            kateg_v = None
        if kateg_v not in KATEG_VALUES:
            errors.append("Выберите категорию (1–3)")
        atype = request.form.get("ALARM_TYPE", "")
        if atype not in ALARM_TYPE_VALUES:
            errors.append("Выберите тип аварии")
        if not codes:
            errors.append("Не выбрано ни одной станции")

        numb_raw = (request.form.get("NUMB_RBS", "1") or "1").strip().upper()
        if numb_raw in ("С", "C"):
            numb = SECTOR
        elif numb_raw.isdigit() and 1 <= int(numb_raw) <= 10000:
            numb = int(numb_raw)
        else:
            numb = None
            errors.append("Количество БС на станцию — число 1–10000 или «С»")

        try:
            begin = parse_dt(request.form.get("BEGIN_TIME"))
            if begin is None:
                errors.append("Укажите начало аварии")
            end_now = request.form.get("END_NOW") == "1"
            end = None if end_now else parse_dt(request.form.get("END_TIME"))
            if not end_now and end is None:
                errors.append("Укажите окончание или отметьте «НВ»")
            if begin and begin > datetime.now():
                errors.append("Начало аварии не может быть в будущем")
            if begin and end and end < begin:
                errors.append("Окончание раньше начала")
        except ValueError as e:
            errors.append(str(e))
            begin = end = None

        secs = {}
        for nm, lbl in (("AKB", "АКБ"), ("DGU", "ГУ"), ("PROSTOY", "Время перерыва связи")):
            try:
                secs[nm] = parse_hms(request.form.get(nm))
            except ValueError as e:
                secs[nm] = None
                errors.append("%s: %s" % (lbl, e))

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            sts = query("SELECT CODE, ADDR, NAPR, TEHC, NETW_TYPE, EQIP_TYPE FROM stations"
                        " WHERE CODE IN (%s)" % ", ".join(["%s"] * len(codes)), codes)
            pw = request.form.get("PW", "").strip()
            descr = request.form.get("DESCR", "").strip()[:450]
            who = request.form.get("ALARM_WHO", "").strip()[:450]
            note = request.form.get("DESC_NOTICE", "").strip()[:450]
            length = fmt_hms(duration_sec(begin, end)) if begin else None
            prostoy_str = fmt_hms(secs["PROSTOY"]) if secs["PROSTOY"] is not None else None
            common_sec = common_prostoy_sec(secs["PROSTOY"], numb)
            common_str = fmt_hms(common_sec) if common_sec is not None else None
            end_val = end if end else ZERO_DT
            akb, dgu = to_sql_time(secs["AKB"]), to_sql_time(secs["DGU"])

            rows_vals = []
            for st in sts:
                rec = {
                    "PW": pw, "NAPR": st["NAPR"], "TEHC": st["TEHC"], "KATEG": kateg_v,
                    "NETW_TYPE": st["NETW_TYPE"], "SERV_ELEM": (st["ADDR"] or "")[:450],
                    "NUMB_RBS": numb, "EQIP_TYPE": (st["EQIP_TYPE"] or "")[:450],
                    "ALARM_TYPE": atype, "DESCR": descr, "BEGIN_TIME": begin,
                    "END_TIME": end_val, "AKB": akb, "DGU": dgu, "LENGHT": length,
                    "PROSTOY": prostoy_str, "COMMON_PROSTOY": common_str,
                    "ALARM_WHO": who, "DESC_NOTICE": note, "STATION_CODE": st["CODE"],
                    "OBJ_TYPE": "Базовая станция",
                }
                rows_vals.append([rec[c] for c in FIELDS])
            if rows_vals:
                with db().cursor() as cur:
                    cur.executemany(
                        "INSERT INTO alarms (%s) VALUES (%s)"
                        % (", ".join(FIELDS), ", ".join(["%s"] * len(FIELDS))), rows_vals)
                audit("bulk_create", None, "%d аварий, тип %s, кат %s"
                      % (len(rows_vals), atype, kateg_v))
                flash("Заведено аварий: %d (тип %s)" % (len(rows_vals), atype), "ok")
            return redirect(url_for("alarms"))

    return render_template("alarms_bulk.html", c=choices(), stations=stations_list, f=f, form=form)


@app.route("/alarms/bulk-close", methods=["GET", "POST"])
@require("alarm_edit")
def alarms_bulk_close():
    """Групповое закрытие: выбрать открытые аварии и закрыть их одним окончанием
    (например, когда восстановили питание в районе)."""
    f = {k: (request.values.get(k, "") or "").strip()
         for k in ("f_napr", "f_tehc", "f_atype", "f_q")}
    conds = ["(END_TIME = '0000-00-00 00:00:00' OR END_TIME IS NULL)"]
    args = []
    if f["f_napr"] in NAPR_VALUES:
        conds.append("NAPR=%s"); args.append(f["f_napr"])
    if f["f_tehc"] in TC_VALUES:
        conds.append("TEHC=%s"); args.append(f["f_tehc"])
    if f["f_atype"] in ALARM_TYPE_VALUES:
        conds.append("ALARM_TYPE=%s"); args.append(f["f_atype"])
    if f["f_q"]:
        conds.append("(SERV_ELEM LIKE %s OR PW LIKE %s OR STATION_CODE LIKE %s)")
        args += ["%" + f["f_q"] + "%"] * 3
    rows = [decorate(r) for r in query(
        SELECT_ALARM + " WHERE " + " AND ".join(conds)
        + " ORDER BY BEGIN_TIME DESC LIMIT 1000", args)]

    form = {}
    if request.method == "POST" and request.form.get("do") == "close":
        form = request.form
        ids = [i for i in request.form.getlist("ids") if i.isdigit()]
        errors = []
        try:
            end = parse_dt(request.form.get("END_TIME"))
            if end is None:
                errors.append("Укажите время окончания")
        except ValueError as e:
            errors.append(str(e)); end = None
        if not ids:
            errors.append("Не выбрано ни одной аварии")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            sel = [decorate(r) for r in query(
                SELECT_ALARM + " WHERE ID IN (%s) AND (END_TIME = '0000-00-00 00:00:00'"
                " OR END_TIME IS NULL)" % ", ".join(["%s"] * len(ids)), ids)]
            closed, skipped = 0, 0
            for r in sel:
                if r["begin"] and end < r["begin"]:
                    skipped += 1
                    continue
                length = fmt_hms(duration_sec(r["begin"], end)) if r["begin"] else None
                execute("UPDATE alarms SET END_TIME=%s, LENGHT=%s WHERE ID=%s",
                        (end, length, r["ID"]))
                closed += 1
            audit("bulk_close", None, "закрыто %d, окончание %s"
                  % (closed, end.strftime("%Y-%m-%d %H:%M")))
            msg = "Закрыто аварий: %d" % closed
            if skipped:
                msg += " (пропущено %d — окончание раньше начала)" % skipped
            flash(msg, "ok")
            return redirect(url_for("alarms"))

    return render_template("alarms_bulk_close.html", c=choices(), rows=rows, f=f, form=form)


@app.route("/alarms/<int:alarm_id>/edit", methods=["GET", "POST"])
@require("alarm_edit")
def alarm_edit(alarm_id):
    row = query(SELECT_ALARM + " WHERE ID=%s", (alarm_id,), one=True)
    if not row:
        abort(404)
    row = decorate(row)
    if request.method == "POST":
        rec, errors = form_to_record(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            sets = ", ".join("%s=%%s" % f for f in FIELDS)
            execute("UPDATE alarms SET %s WHERE ID=%%s" % sets,
                    [rec[f] for f in FIELDS] + [alarm_id])
            audit("update", alarm_id, rec["PW"])
            flash("Авария №%s обновлена" % alarm_id, "ok")
            return redirect(safe_back(request.args.get("back"), url_for("reports")))
        form = request.form
    else:
        form = prefill_from(row)
    return render_template("edit.html", c=choices(), form=form, row=row)


@app.route("/alarms/<int:alarm_id>/delete", methods=["POST"])
@require("alarm_delete")
def alarm_delete(alarm_id):
    execute("DELETE FROM alarms WHERE ID=%s", (alarm_id,))
    audit("delete", alarm_id)
    flash("Авария №%s удалена" % alarm_id, "ok")
    return redirect(safe_back(request.form.get("back"), url_for("reports")))


@app.route("/alarms/<int:alarm_id>/close", methods=["POST"])
@require("alarm_edit")
def alarm_close(alarm_id):
    """Быстрое закрытие аварии: окончание = сейчас (без формы)."""
    row = query(SELECT_ALARM + " WHERE ID=%s", (alarm_id,), one=True)
    if not row:
        abort(404)
    row = decorate(row)
    back = safe_back(request.form.get("back"), url_for("alarms"))
    if not row["is_now"]:
        flash("Авария №%s уже закрыта" % alarm_id, "error")
        return redirect(back)
    now = datetime.now().replace(microsecond=0)
    length = fmt_hms(duration_sec(row["begin"], now)) if row["begin"] else None
    execute("UPDATE alarms SET END_TIME=%s, LENGHT=%s WHERE ID=%s", (now, length, alarm_id))
    audit("close", alarm_id, row["PW"])
    flash("Авария №%s закрыта (окончание — сейчас)" % alarm_id, "ok")
    return redirect(back)


# --- Вкладка «Отчетность» с фильтрами ----------------------------------------
def filtered_rows():
    """Строки за период с учётом фильтров. Возвращает (from, to, rows, filt)."""
    today = datetime.now().strftime("%Y-%m-%d")
    d_from = request.args.get("from") or today
    d_to = request.args.get("to") or today
    try:
        datetime.strptime(d_from, "%Y-%m-%d")
        datetime.strptime(d_to, "%Y-%m-%d")
    except ValueError:
        abort(400)

    conds = ["BEGIN_TIME <= %s",
             "(END_TIME >= %s OR END_TIME = '0000-00-00 00:00:00' OR END_TIME IS NULL)"]
    args = [d_to + " 23:59:59", d_from + " 00:00:00"]

    filt = {}

    def eq(param, column, allowed=None, cast=str):
        v = (request.args.get(param) or "").strip()
        if not v:
            return
        try:
            cv = cast(v)
        except ValueError:
            return
        if allowed is not None and cv not in allowed:
            return
        conds.append("%s = %%s" % column)
        args.append(cv)
        filt[param] = v

    eq("napr", "NAPR", NAPR_VALUES)
    eq("tehc", "TEHC", TC_VALUES)
    eq("kateg", "KATEG", KATEG_VALUES, int)
    eq("netw", "NETW_TYPE", NETW_VALUES)

    # Проблема: группа типов (power/transport/...) или одиночный тип.
    problem = (request.args.get("problem") or "").strip()
    atype = (request.args.get("atype") or "").strip()
    atypes = []
    if problem in PROBLEM_GROUPS:
        atypes = PROBLEM_GROUPS[problem][1]
        filt["problem"] = problem
    elif atype in ALARM_TYPE_VALUES:
        atypes = [atype]
        filt["atype"] = atype
    if atypes:
        conds.append("ALARM_TYPE IN (%s)" % ", ".join(["%s"] * len(atypes)))
        args.extend(atypes)

    status = (request.args.get("status") or "").strip()
    if status == "open":
        conds.append("(END_TIME = '0000-00-00 00:00:00' OR END_TIME IS NULL)")
        filt["status"] = status
    elif status == "closed":
        conds.append("(END_TIME <> '0000-00-00 00:00:00' AND END_TIME IS NOT NULL)")
        filt["status"] = status

    q = (request.args.get("q") or "").strip()
    if q:
        like = "%" + q + "%"
        conds.append("(SERV_ELEM LIKE %s OR DESCR LIKE %s OR DESC_NOTICE LIKE %s"
                     " OR PW LIKE %s OR STATION_CODE LIKE %s)")
        args.extend([like, like, like, like, like])
        filt["q"] = q

    sql = SELECT_ALARM + " WHERE " + " AND ".join(conds) + " ORDER BY BEGIN_TIME DESC"
    rows = [decorate(r) for r in query(sql, args)]
    return d_from, d_to, rows, filt


@app.route("/reports")
@require("report_view")
def reports():
    d_from, d_to, rows, filt = filtered_rows()
    total = sum(common_prostoy_sec(r["PROSTOY_S"], r["NUMB_RBS"]) or 0 for r in rows)
    today = date.today()

    def qs(**over):
        d = request.args.to_dict()
        for k, v in over.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        return urlencode(d)

    quick = [(label, qs(**{"from": a.isoformat(), "to": b.isoformat()}),
              d_from == a.isoformat() and d_to == b.isoformat())
             for label, a, b in (
        ("Сегодня", today, today),
        ("Вчера", today - timedelta(days=1), today - timedelta(days=1)),
        ("7 дней", today - timedelta(days=6), today),
        ("Этот месяц", today.replace(day=1), today),
    )]
    problems = [(key, title, qs(problem=key, atype=None)) for key, (title, _) in PROBLEM_GROUPS.items()]
    stations_hit = len(set(r["STATION_CODE"] for r in rows if r["STATION_CODE"]))
    return render_template("reports.html", rows=rows, d_from=d_from, d_to=d_to,
                           total=fmt_hms(total), back=request.full_path, quick=quick,
                           problems=problems, filt=filt, c=choices(),
                           stations_hit=stations_hit,
                           reset_qs=qs(**{"napr": None, "tehc": None, "kateg": None,
                                          "netw": None, "problem": None, "atype": None,
                                          "status": None, "q": None}),
                           csv_qs=request.query_string.decode("utf-8"),
                           open_count=sum(1 for r in rows if r["is_now"]))


@app.route("/reports.csv")
@require("report_export")
def reports_csv():
    d_from, d_to, rows, _filt = filtered_rows()
    def safe(v):
        # Нейтрализуем формулы Excel/Calc (MOT-054): =, +, -, @, таб, CR
        v = "" if v is None else str(v)
        if v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CSV_HEADERS)
    for r in rows:
        w.writerow([safe(x) for x in (
                    r["ID"], r["PW"], r["NAPR"], r["TEHC"], r["KATEG"],
                    r["NETW_TYPE"], r["SERV_ELEM"], r["numb_txt"], r["EQIP_TYPE"],
                    r["ALARM_TYPE"], r["DESCR"], r["begin_txt"], r["end_txt"],
                    r["akb_txt"], r["dgu_txt"], r["length_txt"], r["prostoy_txt"],
                    r["common_txt"], r["ALARM_WHO"], r["DESC_NOTICE"],
                    r["STATION_CODE"] or "", r["OBJ_TYPE"] or "")])
    audit("export", None, "%s..%s" % (d_from, d_to))
    # BOM — чтобы Excel корректно открыл кириллицу
    data = "﻿" + buf.getvalue()
    # content_type задаётся целиком (mimetype добавил бы charset повторно)
    return Response(data, content_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=report_%s_%s.csv" % (d_from, d_to)})


# --- Импорт CSV --------------------------------------------------------------
# Заголовок CSV (из выгрузки) -> колонка БД. ID на импорте игнорируется.
HEADER_TO_FIELD = {
    "номер pw/ntt": "PW", "направление": "NAPR", "тц": "TEHC",
    "категория": "KATEG", "тип сети": "NETW_TYPE", "сетевой элемент, услуга": "SERV_ELEM",
    "количество бс": "NUMB_RBS", "тип оборудования": "EQIP_TYPE", "тип": "ALARM_TYPE",
    "описание": "DESCR", "начало": "BEGIN_TIME", "окончание": "END_TIME",
    "акб": "AKB", "гу": "DGU", "длительность аварии": "LENGHT",
    "время перерыва связи": "PROSTOY", "общее время простоя бс": "COMMON_PROSTOY",
    "уведомление об аварии (кому)": "ALARM_WHO", "подробное описание": "DESC_NOTICE",
    "код бс (справочник)": "STATION_CODE", "тип объекта": "OBJ_TYPE",
}


def parse_dt_any(v):
    v = (v or "").strip()
    if not v:
        return None
    for f in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S",
              "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(v, f)
        except ValueError:
            pass
    return None


def csv_row_to_rec(cells, col):
    """cells — список значений строки, col — {поле: индекс}. -> (rec, ok)."""
    def g(field):
        i = col.get(field)
        return cells[i].strip() if (i is not None and i < len(cells)) else ""

    begin = parse_dt_any(g("BEGIN_TIME"))
    if begin is None:
        return None, False                 # без начала строку не берём

    end_raw = g("END_TIME")
    end = None if (not end_raw or end_raw.upper() == "НВ") else parse_dt_any(end_raw)

    numb_raw = g("NUMB_RBS").upper()
    if numb_raw in ("С", "C"):
        numb = SECTOR
    elif numb_raw.isdigit() and 1 <= int(numb_raw) <= 10000:
        numb = int(numb_raw)
    else:
        numb = None

    kateg = g("KATEG")
    kateg = int(kateg) if kateg in ("1", "2", "3") else None

    def secs(field):
        try:
            return parse_hms(g(field))
        except ValueError:
            return None

    obj = g("OBJ_TYPE") or "Базовая станция"
    if obj not in OBJ_TYPE_VALUES:
        obj = "Базовая станция"
    length = g("LENGHT") or (fmt_hms(duration_sec(begin, end)) if begin else None)
    prostoy = g("PROSTOY")
    prostoy = prostoy if prostoy else (fmt_hms(secs("PROSTOY")) if secs("PROSTOY") is not None else None)
    common = g("COMMON_PROSTOY") or (
        fmt_hms(common_prostoy_sec(secs("PROSTOY"), numb))
        if (secs("PROSTOY") is not None and numb is not None) else None)

    rec = {
        "PW": g("PW")[:450], "NAPR": g("NAPR")[:450], "TEHC": g("TEHC")[:450],
        "KATEG": kateg, "NETW_TYPE": g("NETW_TYPE")[:450], "SERV_ELEM": g("SERV_ELEM")[:450],
        "NUMB_RBS": numb, "EQIP_TYPE": g("EQIP_TYPE")[:450], "ALARM_TYPE": g("ALARM_TYPE")[:450],
        "DESCR": g("DESCR")[:450], "BEGIN_TIME": begin, "END_TIME": (end if end else ZERO_DT),
        "AKB": to_sql_time(secs("AKB")), "DGU": to_sql_time(secs("DGU")),
        "LENGHT": (length or None), "PROSTOY": prostoy, "COMMON_PROSTOY": common,
        "ALARM_WHO": g("ALARM_WHO")[:450], "DESC_NOTICE": g("DESC_NOTICE")[:450],
        "STATION_CODE": (g("STATION_CODE") or None), "OBJ_TYPE": obj,
    }
    return rec, True


@app.route("/import", methods=["GET", "POST"])
@require("alarm_create")
def import_csv():
    if request.method == "POST":
        mode = request.form.get("mode", "append")
        if mode == "replace" and not can("alarm_delete"):
            abort(403)                      # «начистую» (очистка) — только с правом удаления
        file = request.files.get("file")
        if not file or not (file.filename or "").lower().endswith(".csv"):
            flash("Выберите файл CSV", "error")
            return redirect(url_for("import_csv"))
        text = file.read().decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text), delimiter=";")
        rows = [r for r in reader if any((c or "").strip() for c in r)]
        if len(rows) < 2:
            flash("В файле нет строк данных", "error")
            return redirect(url_for("import_csv"))
        col = {}
        for i, h in enumerate(rows[0]):
            field = HEADER_TO_FIELD.get((h or "").strip().lower())
            if field:
                col[field] = i
        if "BEGIN_TIME" not in col:
            flash("Не найден столбец «Начало» — проверьте, что формат как у выгрузки", "error")
            return redirect(url_for("import_csv"))
        recs, bad = [], 0
        for r in rows[1:]:
            rec, ok = csv_row_to_rec(r, col)
            (recs.append(rec) if ok else None)
            bad += 0 if ok else 1
        with db().cursor() as cur:
            if mode == "replace":
                cur.execute("DELETE FROM alarms")
            if recs:
                cols = ", ".join(FIELDS)
                ph = ", ".join(["%s"] * len(FIELDS))
                cur.executemany("INSERT INTO alarms (%s) VALUES (%s)" % (cols, ph),
                                [[rec[f] for f in FIELDS] for rec in recs])
        audit("import_csv", None, "%s: %d строк, пропущено %d" % (mode, len(recs), bad))
        label = "замена" if mode == "replace" else "добавление"
        msg = "Импорт (%s): загружено %d строк" % (label, len(recs))
        if bad:
            msg += ", пропущено %d (без даты начала)" % bad
        flash(msg, "ok")
        return redirect(url_for("reports"))
    return render_template("import_csv.html")


# --- Вкладка «Статистика» ----------------------------------------------------
@app.route("/stats")
@require("report_view")
def stats():
    now = datetime.now()
    presets = [("24h", "За 24 часа", now - timedelta(hours=24)),
               ("week", "Неделя", now - timedelta(days=7)),
               ("month", "Месяц", now - timedelta(days=30)),
               ("year", "Год", now - timedelta(days=365))]
    rng = (request.args.get("range") or "").strip()
    d_from = (request.args.get("from") or "").strip()
    d_to = (request.args.get("to") or "").strip()
    custom = bool(d_from and d_to)
    if custom:
        try:
            start = datetime.strptime(d_from, "%Y-%m-%d")
            end = datetime.strptime(d_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        except ValueError:
            abort(400)
        active, label = "", "%s — %s" % (d_from, d_to)
    else:
        pm = {p[0]: (p[1], p[2]) for p in presets}
        if rng not in pm:
            rng = "24h"
        label, start = pm[rng]
        end, active = now, rng

    rows = [decorate(r) for r in query(
        SELECT_ALARM + " WHERE BEGIN_TIME <= %s AND (END_TIME >= %s"
        " OR END_TIME = '0000-00-00 00:00:00' OR END_TIME IS NULL) ORDER BY BEGIN_TIME DESC",
        (end.strftime("%Y-%m-%d %H:%M:%S"), start.strftime("%Y-%m-%d %H:%M:%S")))]

    total = len(rows)
    open_n = sum(1 for r in rows if r["is_now"])
    new_n = sum(1 for r in rows if r["begin"] and r["begin"] >= start)
    affected = len(set(r["STATION_CODE"] for r in rows if r["STATION_CODE"]))
    total_sec = sum(common_prostoy_sec(r["PROSTOY_S"], r["NUMB_RBS"]) or 0 for r in rows)
    closed_durs = [duration_sec(r["begin"], r["end"]) for r in rows
                   if not r["is_now"] and r["begin"] and r["end"]]
    avg_len = fmt_hms(sum(closed_durs) // len(closed_durs)) if closed_durs else "—"

    def top(key, n=None):
        c = Counter(r[key] for r in rows if r[key] not in (None, ""))
        return c.most_common(n)

    by_kateg = [("Категория %s" % k, v) for k, v in
                sorted(Counter(r["KATEG"] for r in rows if r["KATEG"]).items())]
    stat = dict(
        total=total, open_n=open_n, closed_n=total - open_n, new_n=new_n,
        affected=affected, total_prostoy=fmt_hms(total_sec), avg_len=avg_len,
        by_kateg=by_kateg, by_type=top("ALARM_TYPE", 10), by_napr=top("NAPR"),
        by_tehc=top("TEHC", 8), by_netw=top("NETW_TYPE"),
        top_st=Counter(r["STATION_CODE"] for r in rows if r["STATION_CODE"]).most_common(6))
    return render_template("stats.html", presets=presets, active=active, custom=custom,
                           label=label, d_from=d_from or start.strftime("%Y-%m-%d"),
                           d_to=d_to or now.strftime("%Y-%m-%d"), **stat)


# --- Справочник БС и история станции -----------------------------------------
@app.route("/stations")
@require("report_view")
def stations():
    q = (request.args.get("q") or "").strip()
    if q:
        like = "%" + q + "%"
        rows = query("SELECT * FROM stations WHERE CODE LIKE %s OR ADDR LIKE %s"
                     " OR TEHC LIKE %s ORDER BY CODE", (like, like, like))
    else:
        rows = query("SELECT * FROM stations ORDER BY CODE")
    counts = {}
    for c in query("SELECT STATION_CODE, COUNT(*) AS n FROM alarms"
                   " WHERE STATION_CODE IS NOT NULL GROUP BY STATION_CODE"):
        counts[c["STATION_CODE"]] = c["n"]
    return render_template("stations.html", rows=rows, counts=counts, q=q)


@app.route("/stations/<code>")
@require("report_view")
def station_card(code):
    st = query("SELECT * FROM stations WHERE CODE=%s", (code,), one=True)
    like = "%" + code + "%"
    rows = [decorate(r) for r in query(
        SELECT_ALARM + " WHERE STATION_CODE=%s OR SERV_ELEM LIKE %s"
        " ORDER BY BEGIN_TIME DESC LIMIT 500", (code, like))]
    total = sum(common_prostoy_sec(r["PROSTOY_S"], r["NUMB_RBS"]) or 0 for r in rows)
    return render_template("station.html", st=st, code=code, rows=rows,
                           total=fmt_hms(total), back=request.full_path,
                           open_n=sum(1 for r in rows if r["is_now"]))


# --- Памятка дежурного -------------------------------------------------------
@app.route("/guide")
@login_required
def guide():
    return render_template("guide.html")


# --- Пользователи ------------------------------------------------------------
@app.route("/users", methods=["GET", "POST"])
@require("user_view")
def users():
    if request.method == "POST":
        if not can("user_manage"):
            abort(403)
        action = request.form.get("action")
        uid = request.form.get("id", type=int)
        target = query("SELECT ROLE FROM users WHERE ID=%s", (uid,), one=True) if uid else None
        if action == "create":
            login_ = request.form.get("login", "").strip()
            pwd = request.form.get("password", "")
            role = request.form.get("role")
            if not login_ or len(pwd) < 6 or role not in assignable_roles():
                flash("Нужны логин, пароль от 6 символов и роль из доступных вам", "error")
            elif query("SELECT ID FROM users WHERE LOGIN=%s", (login_,), one=True):
                flash("Логин уже занят", "error")
            else:
                execute("INSERT INTO users (LOGIN, FULL_NAME, PASSWORD_HASH, ROLE, CREATED_AT)"
                        " VALUES (%s, %s, %s, %s, NOW())",
                        (login_, request.form.get("full_name", "").strip(),
                         generate_password_hash(pwd), role))
                audit("user_create", None, "%s (%s)" % (login_, role))
                flash("Пользователь %s создан" % login_, "ok")
        # Для действий над чужой учёткой — проверяем уровень доступа
        elif action in ("role", "toggle", "password"):
            if not target:
                flash("Пользователь не найден", "error")
            elif uid == session["uid"] or not can_manage_user(target["ROLE"]):
                flash("Недостаточно прав для управления этой учётной записью", "error")
            elif action == "role":
                if request.form.get("role") in assignable_roles():
                    execute("UPDATE users SET ROLE=%s WHERE ID=%s", (request.form["role"], uid))
                    audit("user_role", None, "id=%s -> %s" % (uid, request.form["role"]))
                    flash("Роль изменена", "ok")
                else:
                    flash("Недопустимая роль для вашего уровня", "error")
            elif action == "toggle":
                execute("UPDATE users SET IS_ACTIVE = 1 - IS_ACTIVE WHERE ID=%s", (uid,))
                audit("user_toggle", None, "id=%s" % uid)
                flash("Статус изменен", "ok")
            elif action == "password":
                pwd = request.form.get("password", "")
                if len(pwd) < 6:
                    flash("Пароль — минимум 6 символов", "error")
                else:
                    execute("UPDATE users SET PASSWORD_HASH=%s WHERE ID=%s",
                            (generate_password_hash(pwd), uid))
                    audit("user_password", None, "id=%s" % uid)
                    flash("Пароль изменен", "ok")
        return redirect(url_for("users"))
    # Фильтры списка: поиск по ФИО/логину, должность, статус
    filt = {k: (request.args.get(k) or "").strip() for k in ("q", "role", "status")}
    conds, args = [], []
    if filt["q"]:
        conds.append("(LOGIN LIKE %s OR FULL_NAME LIKE %s)")
        args += ["%" + filt["q"] + "%"] * 2
    if filt["role"] in ROLES:
        conds.append("ROLE=%s"); args.append(filt["role"])
    if filt["status"] == "active":
        conds.append("IS_ACTIVE=1")
    elif filt["status"] == "blocked":
        conds.append("IS_ACTIVE=0")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = query("SELECT ID, LOGIN, FULL_NAME, ROLE, IS_ACTIVE, CREATED_AT FROM users"
                 + where + " ORDER BY LOGIN", args)
    total = query("SELECT COUNT(*) AS n FROM users", one=True)["n"]
    return render_template("users.html", rows=rows, filt=filt, total=total)


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def account_password():
    """Смена СВОЕГО пароля — доступна любому вошедшему пользователю."""
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        me = query("SELECT PASSWORD_HASH FROM users WHERE ID=%s", (session["uid"],), one=True)
        if not me or not check_password_hash(me["PASSWORD_HASH"], current):
            flash("Неверный текущий пароль", "error")
        elif len(new) < 6:
            flash("Новый пароль — минимум 6 символов", "error")
        else:
            execute("UPDATE users SET PASSWORD_HASH=%s WHERE ID=%s",
                    (generate_password_hash(new), session["uid"]))
            audit("self_password")
            flash("Пароль изменён", "ok")
        return redirect(url_for("account_password"))
    return render_template("account_password.html")


@app.route("/audit")
@require("audit_view")
def audit_view():
    rows = query("SELECT * FROM audit_log ORDER BY ID DESC LIMIT 500")
    return render_template("audit.html", rows=rows)


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", msg="Недостаточно прав для этого действия"), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", msg="Запись не найдена"), 404


# Пропускаем для юнит-тестов чистых функций (MOT-005)
if os.environ.get("SKIP_BOOTSTRAP") != "1":
    bootstrap()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
