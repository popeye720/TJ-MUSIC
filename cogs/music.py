import os
import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
import wavelink


# ✅ BRANDING (EXACT)
BRAND_TITLE = "MUSIC PROVIDED BY TEJAS"
BRAND_URL = "https://discord.gg/DVqvtsYNy7"
EMBED_COLOR = discord.Color.from_rgb(2, 102, 255)

log = logging.getLogger("musicbot.music")


def format_duration_ms(ms: int | None) -> str:
    if not ms or ms <= 0:
        return "Unknown"
    total_s = ms // 1000
    m = total_s // 60
    s = total_s % 60
    if m >= 60:
        h = m // 60
        m = m % 60
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


@dataclass
class Track:
    playable: wavelink.Playable
    requester_id: int

    @property
    def title(self) -> str:
        return getattr(self.playable, "title", "Unknown")

    @property
    def author(self) -> str:
        return (
            getattr(self.playable, "author", None)
            or getattr(self.playable, "artist", None)
            or "Unknown"
        )

    @property
    def duration_ms(self) -> int | None:
        return getattr(self.playable, "length", None) or getattr(self.playable, "duration", None)

    @property
    def uri(self) -> str:
        return getattr(self.playable, "uri", "") or getattr(self.playable, "url", "") or ""


class GuildMusicState:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Optional[Track] = None

        # ✅ loop current song
        self.loop_enabled: bool = False

        # ✅ panel storage
        self.panel_channel_id: Optional[int] = None
        self.panel_message_id: Optional[int] = None

        self.player_task: Optional[asyncio.Task] = None
        self.stopped: bool = False

        # ✅ 1-second global (per-guild) button cooldown
        self.cooldown_until: float = 0.0

        # last /play channel (where we post queue ended / idle embeds)
        self.last_play_text_channel_id: Optional[int] = None

        # idle leave control
        self.idle_leave_seconds: int = 120


class MusicPanelView(discord.ui.View):
    def __init__(self, cog: "Music", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if not guild:
            return False

        st = self.cog.get_state(guild.id)

        # ✅ 1 second safety cooldown for everyone (per guild)
        now = time.monotonic()
        if now < st.cooldown_until:
            await self.cog._safe_ephemeral(interaction, "⏳ Wait 1 second...")
            return False

        player = self.cog.get_player(guild)
        if not player or not getattr(player, "connected", False) or not getattr(player, "channel", None):
            await self.cog._safe_ephemeral(interaction, "Bot is not connected to a voice channel.")
            return False

        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await self.cog._safe_ephemeral(interaction, "You must be in the same voice channel as the bot.")
            return False

        if member.voice.channel.id != player.channel.id:
            await self.cog._safe_ephemeral(interaction, "You must be in the same voice channel as the bot.")
            return False

        return True

    @discord.ui.button(label="▶️ Play", style=discord.ButtonStyle.secondary, custom_id="music_play")
    async def btn_play(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._btn_play(interaction)

    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.secondary, custom_id="music_pause")
    async def btn_pause(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._btn_pause(interaction)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="music_skip")
    async def btn_skip(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._btn_skip(interaction)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.secondary, custom_id="music_stop")
    async def btn_stop(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._btn_stop(interaction)

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.secondary, custom_id="music_loop")
    async def btn_loop(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog._btn_loop(interaction)


class Music(commands.Cog):


    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.states: dict[int, GuildMusicState] = {}
        self._voice_locks: dict[int, asyncio.Lock] = {}

        self._node_ready = asyncio.Event()
        self._node_connecting = asyncio.Lock()
        self.music_available: bool = True
        self._node_retry_task: Optional[asyncio.Task] = None
        self._retry_interval = 120
        # ✅ persistent view registration (buttons remain functional on edited panel messages)
        self.bot.add_view(MusicPanelView(self, guild_id=0), message_id=None)

    def _reset_node_state(self):
        self._node_ready.clear()
        self.music_available = False

    # ---------- helpers ----------
    def _get_voice_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._voice_locks:
            self._voice_locks[guild_id] = asyncio.Lock()
        return self._voice_locks[guild_id]

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState()
        return self.states[guild_id]

    def get_player(self, guild: discord.Guild) -> Optional[wavelink.Player]:
        vc = guild.voice_client
        return vc if isinstance(vc, wavelink.Player) else None

    async def _safe_ephemeral(self, interaction: discord.Interaction, content: str):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except Exception:
            pass

    def _hit_cooldown(self, guild_id: int):
        st = self.get_state(guild_id)
        st.cooldown_until = time.monotonic() + 1.0

    async def _connect_node(self) -> None:
        if self._node_ready.is_set():
            return

        async with self._node_connecting:
            if self._node_ready.is_set():
                return

            uri = (os.getenv("LAVALINK_URI") or "").strip().rstrip("/")
            password = (os.getenv("LAVALINK_PASSWORD") or "").strip()

            if not uri or not password:
                self.music_available = False
                return

            try:
                node = wavelink.Node(uri=uri, password=password)
                await wavelink.Pool.connect(nodes=[node], client=self.bot)
                self._node_ready.set()
                self.music_available = True
                print(f"✅ Lavalink node connected → {uri}")
                log.info(f"✅ Lavalink node connected → {uri}")

            except Exception:
                self.music_available = False
                log.exception("Lavalink node connect failed (will retry)")


    async def _node_auto_recovery_loop(self):
        while True:
            await asyncio.sleep(self._retry_interval)
            if self._node_ready.is_set():
                continue
            log.info("🔁 Trying Lavalink auto-recovery...")
            self._reset_node_state()
            try:
                await self._connect_node()
                if self._node_ready.is_set():
                    log.info("✅ Lavalink auto-recovered successfully")
            except Exception:
                log.exception("Lavalink auto-recovery attempt failed")   


    async def ensure_voice(self, interaction: discord.Interaction) -> Optional[wavelink.Player]:
        guild = interaction.guild
        if not guild:
            return None

        await self._connect_node()
        if not self._node_ready.is_set():
            await self._safe_ephemeral(interaction, "⚠️ Music is currently disabled (Lavalink offline or not configured).")
            return None

        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await self._safe_ephemeral(interaction, "You must be in a voice channel.")
            return None

        lock = self._get_voice_lock(guild.id)
        async with lock:
            player = self.get_player(guild)
            if player and getattr(player, "connected", False):
                if player.channel and member.voice.channel.id != player.channel.id:
                    await self._safe_ephemeral(interaction, "Bot is already connected in a different voice channel.")
                    return None
                return player

            try:
                player = await member.voice.channel.connect(cls=wavelink.Player)
                try:
                    await guild.change_voice_state(channel=member.voice.channel, self_mute=False, self_deaf=True)
                except Exception:
                    pass
                return player
            except Exception:
                await self._safe_ephemeral(interaction, "Voice connect failed.")
                return None

    async def _ensure_same_vc(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if not guild:
            return False

        player = self.get_player(guild)
        if not player or not getattr(player, "connected", False) or not getattr(player, "channel", None):
            await self._safe_ephemeral(interaction, "Bot is not connected to a voice channel.")
            return False

        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await self._safe_ephemeral(interaction, "You must be in the same voice channel as the bot.")
            return False

        if member.voice.channel.id != player.channel.id:
            await self._safe_ephemeral(interaction, "You must be in the same voice channel as the bot.")
            return False

        return True

    # ---------- embeds ----------
    def _base_embed(self) -> discord.Embed:
        return discord.Embed(title=BRAND_TITLE, url=BRAND_URL, color=EMBED_COLOR)

    def build_now_playing_embed(self, guild: discord.Guild) -> discord.Embed:
        st = self.get_state(guild.id)
        embed = self._base_embed()

        t = st.current
        if not t:
            embed.description = "No track is playing."
            embed.add_field(name="Loop", value="On" if st.loop_enabled else "Off", inline=True)
            embed.add_field(name="Queue", value=f"{st.queue.qsize()} track(s)" if st.queue.qsize() else "(empty)", inline=True)
            return embed

        title_line = f"**[{t.title}]({t.uri or BRAND_URL})**"
        embed.description = title_line

        embed.add_field(name="Author", value=t.author or "Unknown", inline=True)
        embed.add_field(name="Duration", value=format_duration_ms(t.duration_ms), inline=True)
        embed.add_field(name="Requested By", value=f"<@{t.requester_id}>", inline=True)

        embed.add_field(name="Loop", value="On" if st.loop_enabled else "Off", inline=True)
        embed.add_field(name="Queue", value=f"{st.queue.qsize()} track(s)" if st.queue.qsize() else "(empty)", inline=True)
        return embed

    def build_queue_ended_embed(self) -> discord.Embed:
        embed = self._base_embed()
        embed.description = "All songs have been played! You can add songs again using /play command."
        return embed

    def build_idle_leave_embed(self) -> discord.Embed:
        embed = self._base_embed()
        embed.description = "Leaving voice channel due to inactivity. You can add songs again using /play command."
        return embed

    async def get_panel_message(self, guild: discord.Guild) -> Optional[discord.Message]:
        st = self.get_state(guild.id)
        if not st.panel_channel_id or not st.panel_message_id:
            return None

        ch = guild.get_channel(st.panel_channel_id)
        if not isinstance(ch, discord.TextChannel):
            return None

        try:
            return await ch.fetch_message(st.panel_message_id)
        except Exception:
            return None

    async def set_panel(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        *,
        embed: discord.Embed,
        view: Optional[discord.ui.View],
    ) -> None:
        st = self.get_state(guild.id)

        existing = await self.get_panel_message(guild)
        if existing:
            try:
                await existing.edit(embed=embed, view=view)
                return
            except Exception:
                pass

        msg = await channel.send(embed=embed, view=view)
        st.panel_channel_id = msg.channel.id
        st.panel_message_id = msg.id

    async def refresh_panel(self, guild: discord.Guild, *, keep_buttons: bool = True) -> None:
        msg = await self.get_panel_message(guild)
        if not msg:
            return
        try:
            view = MusicPanelView(self, guild.id) if keep_buttons else None
            await msg.edit(embed=self.build_now_playing_embed(guild), view=view)
        except Exception:
            pass

    async def _send_to_last_play_channel(self, guild: discord.Guild, embed: discord.Embed) -> None:
        st = self.get_state(guild.id)
        ch = guild.get_channel(st.last_play_text_channel_id) if st.last_play_text_channel_id else None
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

    def _ensure_player_loop_running(self, guild: discord.Guild, player: wavelink.Player) -> None:
        st = self.get_state(guild.id)

        if (not st.player_task) or st.player_task.done():
            st.player_task = self.bot.loop.create_task(self.player_loop(guild.id))
            return

        # If task exists but we're idle with no current and not playing/paused, restart for safety.
        if st.current is None and not getattr(player, "playing", False) and not getattr(player, "paused", False):
            try:
                st.player_task.cancel()
            except Exception:
                pass
            st.player_task = self.bot.loop.create_task(self.player_loop(guild.id))

    async def player_loop(self, guild_id: int) -> None:
        st = self.get_state(guild_id)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        while True:
            player = self.get_player(guild)
            if not player or not getattr(player, "connected", False):
                st.current = None
                return

            if st.stopped:
                st.stopped = False
                st.current = None
                st.loop_enabled = False

                while not st.queue.empty():
                    try:
                        st.queue.get_nowait()
                    except Exception:
                        break

                try:
                    await player.stop()
                except Exception:
                    pass
                try:
                    await player.disconnect()
                except Exception:
                    pass

                st.panel_channel_id = None
                st.panel_message_id = None
                return

            try:
                track = await asyncio.wait_for(st.queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue

            while True:
                st.current = track
                try:
                    await player.play(track.playable)
                except Exception:
                    st.current = None
                    # If this track fails, try next immediately
                    if not st.queue.empty():
                        try:
                            track = st.queue.get_nowait()
                            continue
                        except Exception:
                            pass
                    break

                await asyncio.sleep(0.25)
                await self.refresh_panel(guild, keep_buttons=True)

                while getattr(player, "playing", False) or getattr(player, "paused", False):
                    if st.stopped:
                        try:
                            await player.stop()
                        except Exception:
                            pass
                        break
                    await asyncio.sleep(0.75)

                if st.stopped:
                    st.current = None
                    break

                if st.loop_enabled:
                    continue

                st.current = None
                break

            # Queue ended & not looping
            if st.queue.empty() and not st.stopped and not st.loop_enabled:
                msg = await self.get_panel_message(guild)
                if msg:
                    try:
                        await msg.edit(embed=self.build_queue_ended_embed(), view=None)
                    except Exception:
                        pass

                # Idle timeout before leaving
                await asyncio.sleep(st.idle_leave_seconds)

                player = self.get_player(guild)
                if not player or not getattr(player, "connected", False):
                    st.current = None
                    return

                if not st.queue.empty() or getattr(player, "playing", False) or getattr(player, "paused", False):
                    continue

                await self._send_to_last_play_channel(guild, self.build_idle_leave_embed())

                try:
                    await player.disconnect()
                except Exception:
                    pass

                st.current = None
                st.panel_channel_id = None
                st.panel_message_id = None
                return

    # ---------- buttons ----------
    async def _btn_play(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return

        self._hit_cooldown(guild.id)

        player = self.get_player(guild)
        if not player or not getattr(player, "connected", False):
            return await interaction.followup.send("Not connected.", ephemeral=True)

        if getattr(player, "paused", False):
            try:
                await player.pause(False)
            except Exception:
                try:
                    await player.resume()
                except Exception:
                    pass
            await interaction.followup.send("▶️ Resumed.", ephemeral=True)
        else:
            await interaction.followup.send("Already playing.", ephemeral=True)

        await self.refresh_panel(guild, keep_buttons=True)

    async def _btn_pause(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return

        self._hit_cooldown(guild.id)

        player = self.get_player(guild)
        if not player or not getattr(player, "connected", False):
            return await interaction.followup.send("Not connected.", ephemeral=True)

        if getattr(player, "playing", False) and not getattr(player, "paused", False):
            try:
                await player.pause(True)
            except Exception:
                try:
                    await player.pause()
                except Exception:
                    pass
            await interaction.followup.send("⏸️ Paused.", ephemeral=True)
        else:
            await interaction.followup.send("Nothing is playing.", ephemeral=True)

        await self.refresh_panel(guild, keep_buttons=True)

    async def _btn_skip(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return

        self._hit_cooldown(guild.id)

        st = self.get_state(guild.id)
        st.loop_enabled = False

        player = self.get_player(guild)
        if not player or not getattr(player, "connected", False):
            return await interaction.followup.send("Not connected.", ephemeral=True)

        if getattr(player, "playing", False) or getattr(player, "paused", False):
            try:
                await player.stop()
            except Exception:
                pass
            await interaction.followup.send("⏭️ Skipped.", ephemeral=True)
        else:
            await interaction.followup.send("Nothing to skip.", ephemeral=True)

        if st.queue.empty() and st.current is None:
            msg = await self.get_panel_message(guild)
            if msg:
                try:
                    await msg.edit(embed=self.build_queue_ended_embed(), view=None)
                except Exception:
                    pass
        else:
            await self.refresh_panel(guild, keep_buttons=True)

    async def _btn_stop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return

        self._hit_cooldown(guild.id)

        st = self.get_state(guild.id)
        st.stopped = True
        st.loop_enabled = False

        player = self.get_player(guild)
        if player and getattr(player, "connected", False):
            try:
                await player.stop()
            except Exception:
                pass
            try:
                await player.disconnect()
            except Exception:
                pass

        while not st.queue.empty():
            try:
                st.queue.get_nowait()
            except Exception:
                break
        st.current = None

        msg = await self.get_panel_message(guild)
        if msg:
            try:
                await msg.edit(embed=self.build_queue_ended_embed(), view=None)
            except Exception:
                pass

        st.panel_channel_id = None
        st.panel_message_id = None

        await interaction.followup.send("⏹️ Stopped.", ephemeral=True)

    async def _btn_loop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return

        self._hit_cooldown(guild.id)

        st = self.get_state(guild.id)
        st.loop_enabled = not st.loop_enabled

        await interaction.followup.send(f"🔁 Loop: {'On' if st.loop_enabled else 'Off'}", ephemeral=True)
        await self.refresh_panel(guild, keep_buttons=True)

    # ---------- SLASH COMMANDS ----------
    @app_commands.command(name="play", description="Play a song by name or URL")
    @app_commands.describe(query="Song name or URL")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            return await interaction.edit_original_response(content="Guild only.")

        player = await self.ensure_voice(interaction)
        if not player:
            if not self.music_available:
                return await interaction.edit_original_response(content="🛠️ **Music system is currently under maintenance.**\nPlease try again later.")
            return await interaction.edit_original_response(content="Music unavailable.")

        st = self.get_state(guild.id)
        if isinstance(interaction.channel, discord.TextChannel):
            st.last_play_text_channel_id = interaction.channel.id

        q = (query or "").strip()
        if not q:
            return await interaction.edit_original_response(content="Provide a song name or URL.")

        try:
            if q.startswith("http://") or q.startswith("https://"):
                results = await wavelink.Playable.search(q)
            else:
                results = await wavelink.Playable.search(f"ytmsearch:{q}")
                
            playables: list[wavelink.Playable] = []

            if results is None:
                playables = []
            elif isinstance(results, wavelink.Playlist):
                playables = list(results)
                # safety cap for extremely large playlists
                if len(playables) > 200:
                    playables = playables[:200]
            elif isinstance(results, (list, tuple)):
                playables = list(results[:1])
            else:
                playables = [results]

            if not playables:
                return await interaction.edit_original_response(
                    content="No tracks found. (Make sure Lavalink YouTube plugin is enabled.)"
                )

            if st.stopped:
                st.stopped = False

            added = 0
            for p in playables:
                await st.queue.put(Track(playable=p, requester_id=interaction.user.id))
                added += 1

            self._ensure_player_loop_running(guild, player)
            await asyncio.sleep(0.15)

            if isinstance(interaction.channel, discord.TextChannel):
                await self.set_panel(
                    interaction.channel,
                    guild,
                    embed=self.build_now_playing_embed(guild),
                    view=MusicPanelView(self, guild.id),
                )

            await interaction.edit_original_response(content=f"Queued: {added} track(s).")
            await self.refresh_panel(guild, keep_buttons=True)

        except Exception:
            log.exception("/play failed")
            await interaction.edit_original_response(content="Play failed.")

    @app_commands.command(name="stop", description="Stop playback and clear queue (same VC only)")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        if not await self._ensure_same_vc(interaction):
            return
        await self._btn_stop(interaction)

    # ---------- events ----------
@commands.Cog.listener()
async def on_ready(self):
    await self._connect_node()

    # 🔥 CLEAN OLD PANELS ON BOT RESTART
    for guild_id, st in list(self.states.items()):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            continue

        if st.panel_channel_id and st.panel_message_id:
            channel = guild.get_channel(st.panel_channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(st.panel_message_id)
                    # Option A: delete old panel completely
                    await msg.delete()
                except Exception:
                    pass

            # reset stored panel refs
            st.panel_channel_id = None
            st.panel_message_id = None

    # auto-recovery loop
    if not self._node_retry_task or self._node_retry_task.done():
        self._node_retry_task = self.bot.loop.create_task(
            self._node_auto_recovery_loop()
        )


    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        try:
            st = self.states.pop(guild.id, None)
            self._voice_locks.pop(guild.id, None)
            if st and st.player_task and not st.player_task.done():
                st.stopped = True
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
