import discord
from discord.ext import commands

import asyncio
import itertools
import sys
import traceback
from async_timeout import timeout
from functools import partial
from youtube_dl import YoutubeDL

ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

ffmpegopts = {
    'before_options': '-nostdin',
    'options': '-vn'
}

ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
    """Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Exception for cases of invalid Voice Channels."""


class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, download=False):
        loop = loop or asyncio.get_event_loop()

        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        embed = discord.Embed(color=0x001eff)
        embed.add_field(name="Message:", value=f'```ini\n[Added {data["title"]} to the Queue.]\n```', inline=True)
        embed.set_footer(text="Bot by stunning_munda#2359")
        await ctx.send(embed=embed)

        if download:
            source = ytdl.prepare_filename(data)
        else:
            return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

        return cls(discord.FFmpegPCMAudio(source), data=data, requester=ctx.author)

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Used for preparing a stream, instead of downloading.
        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)

        return cls(discord.FFmpegPCMAudio(data['url']), data=data, requester=requester)


class MusicPlayer(commands.Cog):
    """A class which is assigned to each guild using the bot for Music.
    This class implements a queue and loop, which allows for different guilds to listen to different playlists
    simultaneously.
    When the bot disconnects from the Voice it's instance will be destroyed.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None

        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Our main player loop."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    embed = discord.Embed(color=0x001eff)
                    embed.add_field(name="Message:",
                                    value='Please make sure you are in a valid channel or provide me with one!',
                                    inline=True)
                    embed.set_footer(text="Bot by stunning_munda#2359")
                    await self._channel.send(embed=embed)
                    continue

            source.volume = self.volume
            self.current = source

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            np_embed = discord.Embed(color=0x001eff)
            np_embed.add_field(name="Message:",
                               value=f'**Now Playing:** `{source.title}` requested by 'f'`{source.requester}`',
                               inline=True)
            np_embed.set_footer(text="Bot by stunning_munda#2359")
            self.np = await self._channel.send(embed=np_embed)
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            source.cleanup()
            self.current = None

            try:
                # We are no longer playing this song...
                await self.np.delete()
            except discord.HTTPException:
                pass

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command can not be used in Private Messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value='Please make sure you are in a valid channel or provide me with one',
                            inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            return await ctx.send(embed=embed)

        print('Ignoring exception in command {}:'.format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='connect', aliases=['join'])
    async def connect_(self, ctx):
        try:
            channel = ctx.author.voice.channel
        except AttributeError:
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value='No channel to join.', inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            await ctx.send(embed=embed)
            raise InvalidVoiceChannel('No channel to join.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                embed = discord.Embed(color=0x001eff)
                embed.add_field(name="Message:", value=f'Moving to channel: <{channel}> timed out.', inline=True)
                embed.set_footer(text="Bot by stunning_munda#2359")
                await ctx.send(embed=embed)
                raise VoiceConnectionError(f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                embed = discord.Embed(color=0x001eff)
                embed.add_field(name="Message:", value=f'Connecting to channel: <{channel}> timed out.', inline=True)
                embed.set_footer(text="Bot by stunning_munda#2359")
                await ctx.send(embed=embed)
                raise VoiceConnectionError(f'Connecting to channel: <{channel}> timed out.')

        embed = discord.Embed(color=0x001eff)
        embed.add_field(name="Message:", value=f':white_check_mark: Connected to: **{channel}** :white_check_mark:',
                        inline=True)
        embed.set_footer(text="Bot by stunning_munda#2359")
        await ctx.send(embed=embed)

    @commands.command(name='play', aliases=['sing'])
    async def play_(self, ctx, *, search: str):
        await ctx.trigger_typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        # If download is False, source will be a dict which will be used later to regather the stream.
        # If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=False)

        await player.queue.put(source)

    @commands.command(name='pause')
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value='I am not currently playing anything!', inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            return await ctx.send(embed=embed)
        elif vc.is_paused():
            return

        vc.pause()
        embed = discord.Embed(color=0x001eff)
        embed.add_field(name="Message:", value=f'**`{ctx.author}`**: Paused the song!', inline=True)
        embed.set_footer(text="Bot by stunning_munda#2359")
        await ctx.send(embed=embed)

    @commands.command(name='resume')
    async def resume_(self, ctx):
        """Resume the currently paused song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value='I am not currently playing anything!', inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            return await ctx.send(embed=embed)
        elif not vc.is_paused():
            return

        vc.resume()
        embed = discord.Embed(color=0x001eff)
        embed.add_field(name="Message:", value=f'**`{ctx.author}`**: Resumed the song!', inline=True)
        embed.set_footer(text="Bot by stunning_munda#2359")
        await ctx.send(embed=embed)

    @commands.command(name='skip')
    async def skip_(self, ctx):
        """Skip the song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value='I am not currently playing anything!', inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            return await ctx.send(embed=embed)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()
        embed = discord.Embed(color=0x001eff)
        embed.add_field(name="Message:", value=f'**`{ctx.author}`**: Skipped the song!', inline=True)
        embed.set_footer(text="Bot by stunning_munda#2359")
        await ctx.send(embed=embed)

    @commands.command(name='queue', aliases=['q', 'playlist'])
    async def queue_info(self, ctx):
        """Retrieve a basic queue of upcoming songs."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value='I am not currently connected to voice!', inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        if player.queue.empty():
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value='There are currently no more queued songs.', inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            return await ctx.send(embed=embed)

        # Grab up to 5 entries from the queue...
        upcoming = list(itertools.islice(player.queue._queue, 0, 5))

        fmt = '\n'.join(f'**`{_["title"]}`**' for _ in upcoming)
        embed = discord.Embed(title=f'Upcoming - Next {len(upcoming)}', description=fmt)

        await ctx.send(embed=embed)

    @commands.command(name='now_playing', aliases=['np', 'current', 'currentsong', 'playing'])
    async def now_playing_(self, ctx):
        """Display information about the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value='I am not currently connected to voice!', inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        if not player.current:
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value='I am not currently playing anything!', inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            return await ctx.send(embed=embed)

        try:
            # Remove our previous now_playing message.
            await player.np.delete()
        except discord.HTTPException:
            pass

        embed = discord.Embed(color=0x001eff)
        embed.add_field(name="Message:",
                        value=f'**Now Playing:** `{vc.source.title}` 'f'requested by `{vc.source.requester}`',
                        inline=True)
        embed.set_footer(text="Bot by stunning_munda#2359")

        player.np = await ctx.send(embed=embed)

    @commands.command(name='volume', aliases=['vol'])
    async def change_volume(self, ctx, *, vol: float):
        """Change the player volume.
        Parameters
        ------------
        volume: float or int [Required]
            The volume to set the player to in percentage. This must be between 1 and 100.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value="I am not currently connected to voice!", inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            await ctx.send(embed=embed)

        if not 0 < vol < 101:
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value="Please enter a value between 1 and 100.", inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            await ctx.send(embed=embed)

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        embed = discord.Embed(color=0x001eff)
        embed.add_field(name="Message:", value=f'**`{ctx.author}`**: Set the volume to **{vol}%**', inline=True)
        embed.set_footer(text="Bot by stunning_munda#2359")
        await ctx.send(embed=embed)
        await ctx.send()

    @commands.command(name='stop', aliases=['leave'])
    async def stop_(self, ctx):
        """Stop the currently playing song and destroy the player.
        !Warning!
            This will destroy the player assigned to your guild, also deleting any queued songs and settings.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(color=0x001eff)
            embed.add_field(name="Message:", value="I am not currently playing anything!", inline=True)
            embed.set_footer(text="Bot by stunning_munda#2359")
            return await ctx.send(embed=embed)

        await self.cleanup(ctx.guild)

    @commands.command()
    async def server_info(self, ctx):
        name = str(ctx.guild.name)
        description = str(ctx.guild.description)

        owner = str(ctx.guild.owner)
        id = str(ctx.guild.id)
        region = str(ctx.guild.region)
        memberCount = str(ctx.guild.member_count)

        icon = str(ctx.guild.icon_url)

        embed = discord.Embed(
            title=name + " Server Information",
            description=description,
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=icon)
        embed.add_field(name="Owner", value=owner, inline=True)
        embed.add_field(name="Server ID", value=id, inline=True)
        embed.add_field(name="Region", value=region, inline=True)
        embed.add_field(name="Member Count", value=memberCount, inline=True)

        await ctx.send(embed=embed)

def setup(bot):
    bot.add_cog(Music(bot))
