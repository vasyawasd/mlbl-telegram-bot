#!/usr/bin/env bash
# Неубиваемый запуск Telegram-бота МЛБЛ с супервизором и защитой от сна
set -e
cd "$(dirname "$0")"

# Включение wake-lock для Termux (предотвращает сон процессора Android при выключенном экране)
if command -v termux-wake-lock &> /dev/null; then
    echo "🔋 [Termux] Активация termux-wake-lock..."
    termux-wake-lock
fi

# Определение интерпретатора Python
PYTHON_BIN="python3"
if ! command -v python3 &> /dev/null; then
    PYTHON_BIN="python"
fi

echo "🚀 Запуск неубиваемого супервизора МЛБЛ бота..."
exec $PYTHON_BIN supervisor.py
