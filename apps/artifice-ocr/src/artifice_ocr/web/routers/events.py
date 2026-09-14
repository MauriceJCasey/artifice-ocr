# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SSE event stream for live progress updates."""

import asyncio
import json
import queue

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..runtime import state
from ..serializers import serialize_event

router = APIRouter(tags=["events"])


async def _event_stream():
    """Drain the runner's queue.Queue and forward each event as an SSE frame."""
    while True:
        runner = state.runner
        if runner is None:
            await asyncio.sleep(0.3)
            yield ": waiting for a run to start\n\n"
            continue

        try:
            event = await asyncio.to_thread(runner.events.get, True, 1.0)
        except queue.Empty:
            yield ": heartbeat\n\n"
            continue

        if event.kind == "item_finished" and event.item is not None:
            state.record_finished_item(event.item)
        if event.kind == "run_finished":
            state.finish_run(event.payload)

        yield f"data: {json.dumps(serialize_event(event))}\n\n"


@router.get("/api/events")
async def events():
    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
