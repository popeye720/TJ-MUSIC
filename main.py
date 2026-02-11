import os
import asyncio
import traceback

import discord
from discord.ext import commands


class TejasBot(commands.Bot):
    async def setup_hook(self) -> None:
        try:
            await self.load_extension("cogs.music")
        except Exception as e:
            print("❌ Failed to load cogs.music:", e)
            traceback.print_exc()

        try:
            await self.tree.sync()
            print("✅ Slash commands synced.")
        except Exception as e:
            print("❌ Slash command sync failed:", e)
            traceback.print_exc()


def main() -> None:
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        print("❌ Missing DISCORD_TOKEN env var.")
        return

    intents = discord.Intents.default()
    intents.guilds = True
    intents.voice_states = True

    bot = TejasBot(command_prefix="!", intents=intents)

    try:
        bot.run(token)
    except Exception as e:
        print("❌ Bot crashed:", e)
        traceback.print_exc()


if __name__ == "__main__":
    main()
