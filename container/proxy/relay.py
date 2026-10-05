"""The byte relay every egress program shares."""

import asyncio


async def pipe(reader, writer, idle=None):
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), idle)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        close(writer)


def close(*writers):
    for w in writers:
        if w is not None:
            try:
                w.close()
            except OSError:
                pass
