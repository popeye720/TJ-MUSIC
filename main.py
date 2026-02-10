import os
import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("musicbot")


class TejasBot(commands.Bot):
    async def setup_hook(self) -> None:
        # Load cogs dynamically (no music logic here)
        await self.load_extension("cogs.music")

        # Sync slash commands
        try:
            await self.tree.sync()
        except Exception:
            log.exception("Slash command sync failed")


def main() -> None:
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN env var")

    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True  # required for voice connect checks

    bot = TejasBot(
        command_prefix=commands.when_mentioned,  # no message commands used
        intents=intents,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )

    @bot.event
    async def on_ready() -> None:
        log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")

    asyncio.run(bot.start(token))


if __name__ == "__main__":
    main()
