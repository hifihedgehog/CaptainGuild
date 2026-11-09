"""Starts the bot and the website in one process.

    python -m captainguild --public-url https://your.site --port 8893 --data data

.env holds secrets only. Everything else comes from these flags or the dashboard. `--demo` runs the dashboard alone on sample
data, with no Discord connection and no sign-in, for trying pages out and for the browser checks.

`--term-matching` and `--term-configs` name an outside module and its folder of word lists, which screen what Wiki answers sends
and posts. They're flags rather than a dashboard setting because the module is code the bot runs. core/answers/screening.py says
what the module has to provide.

`--answers-servers` lists the servers where Wiki answers may spend the host's AI key, by ID. Without it, no server can switch the
pack on.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import discord
from aiohttp import web
from dotenv import load_dotenv

from . import __version__
from .bot import Bot
from .core.answers import screening, servers
from .core.invite import ADD_PERMISSIONS, invite_url  # noqa: F401  (ADD_PERMISSIONS is read through this module)
from .crypto import Keyring, decode_master_key
from .db import Database, DatabaseKeyError
from .modules import load_all
from .web.app import build_app

log = logging.getLogger("captainguild")

# The exit code that tells a process manager to leave the bot stopped: pm2's --stop-exit-codes, or RestartPreventExitStatus in a systemd unit. A restart can't fix these causes, and each try spends a login.
EXIT_NO_RESTART = 78
WEB_STOP_WAIT = 5.0


def site_runner(app: web.Application) -> web.AppRunner:
    """The website's server. A page stuck on a call to Discord gets a few seconds at a stop, because what memory holds is saved after this and the service manager allows a stop 90 seconds."""
    return web.AppRunner(app, access_log=None, shutdown_timeout=WEB_STOP_WAIT)


def clean_public_url(value: str) -> str:
    """The site's address, ready to put a path after. Raises ValueError with the sentence to show."""
    if not value.startswith(("http://", "https://")):
        raise ValueError("--public-url must start with http:// or https://")
    return value.rstrip("/")


async def gateway_access(token: str) -> tuple[bool, bool]:
    """Whether Discord has given this app the Message Content and Presence intents. Asking for one without having it closes the connection.

    The bot can't run without Server Members, so that raises here, before the site starts and an installer's health check reports success.
    """
    client = discord.Client(intents=discord.Intents(guilds=True))  # it never connects, and the library warns about a client with no guilds intent
    try:
        await client.login(token)
        flags = (await client.application_info()).flags
        if not (flags.gateway_guild_members or flags.gateway_guild_members_limited):
            raise discord.PrivilegedIntentsRequired(None)
        message_text = bool(flags.gateway_message_content or flags.gateway_message_content_limited)
        presences = bool(getattr(flags, "gateway_presence", False) or getattr(flags, "gateway_presence_limited", False))
        return message_text, presences
    finally:
        await client.close()


async def can_read_message_text(token: str) -> bool:
    """Whether Discord has given this app the Message Content intent."""
    return (await gateway_access(token))[0]


async def run(args: argparse.Namespace) -> None:
    data_dir = Path(args.data).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    keys = Keyring(decode_master_key(os.environ["MASTER_KEY"]))
    db = Database(data_dir / "main.db", keys.database_key("main"))
    message_db = Database(data_dir / "messages.db", keys.database_key("messages"))
    bot = runner = stop_waiter = None
    try:
        # These open in here because an open database keeps the process alive, so one that opened must close when the other can't.
        await db.open()
        await message_db.open()
        token = os.environ["DISCORD_TOKEN"]
        read_message_text, read_presences = await gateway_access(token)
        if not read_message_text:
            log.warning("Discord hasn't given this app Message Content, so anything that reads what members type waits for it. Restart the bot once it has.")
        if not read_presences:
            log.info("Discord hasn't given this app the Presence intent, so statistics count members present from Discord's estimate and track no games. Restart the bot once it has.")
        bot = Bot(db, message_db, public_url=args.public_url, read_message_text=read_message_text, read_presences=read_presences)
        await bot.prepare(load_all())
        await bot.login(token)
        application = await bot.application_info()
        if application.bot_public:
            log.warning("Public Bot is on in the Discord Developer Portal, so anyone can add this bot to their server.")
        log.info("Link to add this bot to a server: %s", invite_url(application.id))
        app = build_app(bot, session_key=keys.session_key(), client_id=str(application.id),
                        client_secret=os.environ.get("DISCORD_CLIENT_SECRET", ""), public_url=args.public_url)
        runner = site_runner(app)
        await runner.setup()
        await web.TCPSite(runner, args.host, args.port).start()
        log.info("Captain Guild %s serving %s on %s:%s", __version__, args.public_url, args.host, args.port)
        connection = asyncio.ensure_future(bot.connect(reconnect=True))
        stopping = asyncio.Event()
        for name in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().add_signal_handler(name, stopping.set)
            except NotImplementedError:  # Windows has no signal handlers on the event loop
                pass
        stop_waiter = asyncio.ensure_future(stopping.wait())
        await asyncio.wait({connection, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if connection.done():
            connection.result()  # a failed login or gateway error ends the process, so the process manager restarts it
        log.info("Shutting down")
    finally:
        try:
            if stop_waiter is not None:
                stop_waiter.cancel()
            if runner is not None:
                await runner.cleanup()
            if bot is not None:
                await bot.close()
        finally:
            try:
                await db.close()
            finally:
                await message_db.close()


async def run_demo(args: argparse.Namespace) -> None:
    from .demo import build_demo

    data_dir = Path(args.data).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    bot, app, closers = await build_demo(data_dir)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()
    log.info("Demo dashboard at http://%s:%s/dashboard/%s", args.host, args.port, bot.ops.DEMO_GUILD_ID)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        for closer in closers:
            await closer()


def load_term_matching(module_path: str | None, folder: str | None) -> None:
    """Load the word lists the flags name. One that doesn't load leaves each server screened by its automod word rule, and the Wiki answers page says why."""
    if not module_path or not folder:
        return
    try:
        screening.use(screening.load(module_path, folder))
        log.info("AI answers are screened with the word lists in %s", folder)
    except screening.ScreeningError as error:
        screening.use(None, str(error))
        log.error("The term-matching module didn't load, so each server's automod word rule screens AI answers instead. %s", error)


def main() -> None:
    os.umask(0o077)  # what this program writes stays readable by its own user alone, whatever the shell's default is
    parser = argparse.ArgumentParser(prog="captainguild")
    parser.add_argument("--public-url", default="", help="This site's address, with https and no trailing slash")
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on. Keep this local and put a proxy in front.")
    parser.add_argument("--port", type=int, default=8893)
    parser.add_argument("--data", default="data", help="Folder for the databases")
    parser.add_argument("--demo", action="store_true", help="Run the dashboard alone on sample data")
    parser.add_argument("--term-matching", metavar="FILE", help="A term-matching module whose word lists screen AI answers, loaded once at start")
    parser.add_argument("--term-configs", metavar="FOLDER", help="That module's folder of word lists, one <server ID>.json for each server")
    parser.add_argument("--answers-servers", metavar="IDS", default="", help="The servers where Wiki answers may spend the AI key, by ID, separated by commas")
    args = parser.parse_args()
    if bool(args.term_matching) != bool(args.term_configs):
        parser.error("--term-matching and --term-configs go together")
    try:
        servers.use(servers.parse(args.answers_servers))
    except ValueError as error:
        parser.error(f"--answers-servers: {error}")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    if args.demo:
        asyncio.run(run_demo(args))
        return
    load_term_matching(args.term_matching, args.term_configs)
    if not args.public_url:
        sys.exit("--public-url is required")
    try:
        args.public_url = clean_public_url(args.public_url)
    except ValueError as error:
        sys.exit(str(error))
    missing = [name for name in ("DISCORD_TOKEN", "MASTER_KEY") if not os.environ.get(name)]
    if missing:
        sys.exit(f"Missing from .env: {', '.join(missing)}")
    try:
        asyncio.run(run(args))
    except DatabaseKeyError as error:
        log.critical("%s", error)
        sys.exit(EXIT_NO_RESTART)
    except discord.LoginFailure:
        log.critical("Discord refused the bot token. Put the right one in DISCORD_TOKEN, then start the bot again.")
        sys.exit(EXIT_NO_RESTART)
    except discord.PrivilegedIntentsRequired:
        log.critical("Discord refused a privileged intent. Turn on Server Members in the Developer Portal, then start again.")
        sys.exit(EXIT_NO_RESTART)


if __name__ == "__main__":
    main()
