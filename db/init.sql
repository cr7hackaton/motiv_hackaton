-- Схема БД «Веб отчетность» (MariaDB 10.1)
-- Таблица alarms повторяет структуру из ТЗ. Добавлен только служебный
-- первичный ключ ID — без него невозможно однозначно редактировать строку.

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS web_report CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE web_report;

CREATE TABLE IF NOT EXISTS alarms (
    ID INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    PW VARCHAR(450),
    NAPR VARCHAR(450),
    TEHC VARCHAR(450),
    KATEG TINYINT,
    NETW_TYPE VARCHAR(450),
    SERV_ELEM VARCHAR(450),
    NUMB_RBS INT,
    EQIP_TYPE VARCHAR(450),
    ALARM_TYPE VARCHAR(450),
    DESCR VARCHAR(450),
    BEGIN_TIME DATETIME,
    END_TIME DATETIME,
    AKB TIME,
    DGU TIME,
    LENGHT VARCHAR(450),
    -- Хранятся строкой «чч.мм.сс» без ограничения 838 ч (согласовано с экспертом, MOT-004)
    PROSTOY VARCHAR(450),
    COMMON_PROSTOY VARCHAR(450),
    ALARM_WHO VARCHAR(450),
    DESC_NOTICE VARCHAR(450),
    -- Служебная связь со справочником БС (для автозаполнения и истории станции)
    STATION_CODE VARCHAR(64) NULL,
    -- Тип объекта аварии (станция / контроллер / узел …); nullable-расширение
    OBJ_TYPE VARCHAR(64) NULL,
    KEY idx_begin (BEGIN_TIME),
    KEY idx_station (STATION_CODE),
    KEY idx_atype (ALARM_TYPE)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Справочник базовых станций (адрес, направление, ТЦ) — источник автозаполнения
CREATE TABLE IF NOT EXISTS stations (
    ID INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    CODE VARCHAR(64) NOT NULL UNIQUE,
    ADDR VARCHAR(450),
    NAPR VARCHAR(450),
    TEHC VARCHAR(450),
    NETW_TYPE VARCHAR(450),
    EQIP_TYPE VARCHAR(450)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Пользователи и роли: admin (администратор), duty (дежурный), engineer (инженер)
CREATE TABLE IF NOT EXISTS users (
    ID INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    LOGIN VARCHAR(64) NOT NULL UNIQUE,
    FULL_NAME VARCHAR(255),
    PASSWORD_HASH VARCHAR(255) NOT NULL,
    ROLE VARCHAR(16) NOT NULL,
    IS_ACTIVE TINYINT NOT NULL DEFAULT 1,
    CREATED_AT DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Журнал действий (прозрачность ведения отчетности, п. 4.1 ТЗ)
CREATE TABLE IF NOT EXISTS audit_log (
    ID INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    TS DATETIME NOT NULL,
    USER_LOGIN VARCHAR(64),
    ACTION VARCHAR(32),
    ALARM_ID INT,
    DETAILS VARCHAR(1000),
    KEY idx_ts (TS)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
