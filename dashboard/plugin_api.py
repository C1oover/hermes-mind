from __future__ import annotations
import sys
from pathlib import Path
from typing import Dict
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

# Ensure plugin root is importable even when loaded via `importlib.util.spec_from_file_location`
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from daemon import PLUGIN_DIR, BridgeError, get_mind_runtime  # noqa: E402

router = APIRouter()


class EventPayload(BaseModel):
    kind: str  # "preset" | "nudge" | "set"
    target: str
    value: float = 1.0


@router.get("/status")
async def get_live_status():
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    return rt.status_dict()


@router.post("/tick")
async def force_live_tick():
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    rt.tick_once()
    return rt.status_dict()


@router.post("/event")
async def trigger_live_event(req: EventPayload):
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer ()
    try:
        with rt.lock:
            if req.kind == "preset":
                rt.engine.apply_preset(req.target, req.value)
            elif req.kind == "set":
                rt.engine.set_leaf(req.target, req.value)
            elif req.kind == "nudge":
                rt.engine.nudge(req.target, req.value)
            else:
                raise HTTPException(400, f"Unknown kind {req.kind}")
            rt.save_state()
    except BridgeError as exc:
        raise HTTPException(400, str(exc))
    return rt.status_dict()


@router.put("/traits")
async def update_live_traits(traits: Dict[str, float]):
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    with rt.lock:
        for k, v in traits.items():
            if k in rt.engine.traits:
                t_ir = rt.engine.ir.traits[k]
                rt.engine.set_trait(k, max(t_ir.min_val, min(t_ir.max_val, float(v))))
        rt.save_state()
    return rt.status_dict()


@router.put("/params")
async def update_live_params(params: Dict[str, float]):
    rt = get_mind_runtime()
    rt.reload_from_disk_if_newer()
    with rt.lock:
        for k, v in params.items():
            if k in rt.engine.params:
                rt.engine.set_param(k, float(v))
        rt.save_state()
    return rt.status_dict()


@router.get("/workbench", response_class=HTMLResponse)
async def serve_workbench_html():
    html_path = PLUGIN_DIR / "web" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/mind.js")
async def serve_mind_js():
    return FileResponse(PLUGIN_DIR / "web" / "mind.js", media_type="application/javascript")


@router.get("/model/model.mind")
@router.get("/model.mind")
async def serve_model_mind():
    return FileResponse(PLUGIN_DIR / "model" / "model.mind", media_type="text/plain")

