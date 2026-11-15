"""Работа с СУБД MariaDB 10.1: подключение, запросы, начальная настройка схемы.

Параметры подключения берутся из переменных окружения (см. docker-compose.yml и .env.example).
Схема таблиц создаётся при первом запуске контейнера БД из db/init.sql.
"""
import os
import time

import pymysql
from flask import g
from werkzeug.security import generate_password_hash


def connect():
    """Новое соединение с БД (строки возвращаются словарями, autocommit включён)."""
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
    """Соединение текущего запроса Flask (одно на запрос, закрывается в close_db)."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app):
    """Подключить закрытие соединения к жизненному циклу приложения Flask."""
    app.teardown_appcontext(close_db)


def query(sql, args=None, one=False):
    """SELECT: список строк-словарей, либо одна строка (one=True) или None."""
    with db().cursor() as cur:
        cur.execute(sql, args if args is not None else ())
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=None):
    """INSERT/UPDATE/DELETE: возвращает ID вставленной строки (для INSERT)."""
    with db().cursor() as cur:
        cur.execute(sql, args if args is not None else ())
        return cur.lastrowid


def bootstrap(stations_seed):
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
                " VALUES (%s, %s, %s, %s, %s, %s)", stations_seed)
        cur.execute("SELECT COUNT(*) AS n FROM users")
        if cur.fetchone()["n"] == 0:
            cur.execute(  # первый аккаунт — Владелец (единственный)
                "INSERT IGNORE INTO users (LOGIN, FULL_NAME, PASSWORD_HASH, ROLE, CREATED_AT)"
                " VALUES (%s, %s, %s, 'owner', NOW())",
                (os.environ.get("ADMIN_LOGIN", "admin"), "Админ Админов Админович",
                 generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin"))))
    conn.close()
