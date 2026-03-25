#!/usr/bin/env python3
"""
Market Monitor Bot — 多 Bot 協調架構
Railway 1 個服務，運行 7 個 Discord Bot（分散式執行緒）
"""
from src.discord_bot import main

if __name__ == "__main__":
    main()
