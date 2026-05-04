"""Entry point for nanobot: python -m nanobot [options]"""

import argparse
import asyncio
import logging
from pathlib import Path

from benchclaw import __art__, __version__
from benchclaw.agent.loop import AgentLoop
from benchclaw.bus import MessageBus
from benchclaw.channels.manager import ChannelManager
from benchclaw.config import ConfigManager
from benchclaw.media import MediaRepository
from benchclaw.providers.litellm_provider import LiteLLMProvider
from benchclaw.providers.scripted import ScriptedProvider


def run(args) -> None:
    """Start the BenchClaw process"""
    print(__art__ + f"{__version__:>51s}")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR)

    bus = MessageBus()

    with ConfigManager(args.config) as config:
        if config.provider.name == "scripted":
            fixture = config.provider.api_base or ""
            if not fixture:
                raise SystemExit(
                    "provider.name=scripted requires provider.api_base set to a fixture path"
                )
            provider = ScriptedProvider.from_fixture(Path(fixture).expanduser())
        else:
            provider = LiteLLMProvider(config.provider)

        async def run():
            async with MediaRepository(config.workspace_path) as media_repo:
                channels = ChannelManager(config, bus, media_repo=media_repo)
                agent = AgentLoop(
                    config=config,
                    bus=bus,
                    provider=provider,
                    debug_dump_path=args.debug_dump,
                    media_repo=media_repo,
                )

                print("BenchClaw starting")
                if channels.channels:
                    print(f"Channels: {', '.join(channels.channels)}")
                else:
                    print("Warning: no channels enabled")

                try:
                    async with channels:
                        await agent.run()
                except KeyboardInterrupt:
                    print("\nShutting down...")
                except asyncio.CancelledError:
                    return

        asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nanobot",
        description="nanobot — personal AI assistant gateway",
    )
    parser.add_argument(
        "--config", type=Path, default="config/config.yaml", help="config.yaml file to use"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable info logging",
    )
    parser.add_argument(
        "--debug-dump",
        type=Path,
        default=None,
        metavar="FILE",
        help="dump LLM input messages to this file before each call (for debugging)",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
