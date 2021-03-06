#!/usr/bin/env bash
# Скрипт запускающий бота TG_AutoPoster с поддержкой виртуального окружения virtualenv
# Вы можете запускать данный скрипт по расписанию с помощью crontab
# Для отключения вывода информации в консоль добавьте > /dev/null 2>&1

ENV_PATH="venv" # Путь к папке с виртуальным окружением
PYTHON_EXECUTABLE="python3" # Имя файла интерпретатора Python

if [[ $1 = 'edit' ]]
then
    nano ./config.ini
else
    if [[ -d ${ENV_PATH} ]]
    then
        echo "Активация виртуального окружения."
        source ${ENV_PATH}/bin/activate
        echo "Запуск бота."
        if ! ${PYTHON_EXECUTABLE} TG_AutoPoster.py "$1 $2 $3 $4"
        then
            echo -e "\e[41mПрограмма завершилась неудачно. Смотрите логи.\e[0m"
        fi
        echo "Бот завершил свою работу. Деактивация виртуального окружения."
        echo "Выход."
        deactivate
    else
        echo "Папка с виртуальным окружением не найдена или задана не правильно."
        echo "Попытка запуска бота без виртуального окружения."
        if ! ${PYTHON_EXECUTABLE} TG_AutoPoster.py "$1"
        then
            echo -e "\e[41mПрограмма завершилась неудачно. Смотрите логи.\e[0m"
        fi
        echo "Бот завершил свою работу. Выход."
    fi
fi
