"""Entry point for teachclaw: python -m teachclaw [options]"""

import argparse
import asyncio
import logging
from pathlib import Path

from teachclaw import __art__, __version__
from teachclaw.agent.loop import AgentLoop
from teachclaw.bus import MessageBus
from teachclaw.channels.manager import ChannelManager
from teachclaw.config import ConfigManager
from teachclaw.media import MediaRepository
from teachclaw.providers.litellm_provider import LiteLLMProvider
from teachclaw.providers.scripted import ScriptedProvider


def run(args) -> None:
    """Start the TeachClaw process"""
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
            shared_roots = {
                alias: Path(root).expanduser() for alias, root in config.media.shared_roots.items()
            }
            async with MediaRepository(
                config.workspace_path,
                shared_roots=shared_roots,
                max_age_days=config.media.max_age_days,
            ) as media_repo:
                channels = ChannelManager(config, bus, media_repo=media_repo)
                agent = AgentLoop(
                    config=config,
                    bus=bus,
                    provider=provider,
                    debug_dump_dir=args.debug_dump,
                    media_repo=media_repo,
                )

                print("TeachClaw starting")
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
        prog="teachclaw",
        description="TeachClaw — personal AI assistant gateway",
    )
    parser.add_argument(
        "--config", type=Path, default="config.yaml", help="config.yaml file to use"
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
        metavar="DIR",
        help="dump LLM input messages to this directory (one file per conversation) before each call (for debugging)",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
