"""Точка входа: запуск бота."""
import asyncio
from bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
