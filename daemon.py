"""Mind-Hermes runtime: resident ODE state shared by Hermes hooks, dashboard API,
and (optionally) proactive gateway wakes.

The ODE engine is the authoritative JavaScript framework (web/mind.js) driven
through a resident Node subprocess (js/bridge_server.mjs, JSON-lines RPC).
No Python re-port: single source of truth, exact parity with the Workbench.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from sensors import SensorConfig, TelemetryCollector  # noqa: E402

DEFAULT_MODEL_PATH = PLUGIN_DIR / "model" / "model.mind"
BRIDGE_PATH = PLUGIN_DIR / "js" / "bridge_server.mjs"


def get_data_dir() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    d = hermes_home / "plugin-data" / "mind-hermes"
    d.mkdir(parents=True, exist_ok=True)
    return d


class BridgeError(RuntimeError):
    pass


class NodeBridge:
    """JSON-lines RPC client to js/bridge_server.mjs with auto-restart."""

    def __init__(self, model_path: Path):
        self.model_path = model_path
        self._proc: Optional[subprocess.Popen] = None
        self._rpc_lock = threading.Lock()

    def _spawn(self) -> None:
        node = shutil.which("node")
        if not node:
            raise BridgeError("node interpreter not found on PATH")
        env = dict(os.environ)
        env["MIND_MODEL_PATH"] = str(self.model_path)
        self._proc = subprocess.Popen(
            [node, str(BRIDGE_PATH)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True,
        )
        ready = self._proc.stdout.readline()
        try:
            info = json.loads(ready)
        except Exception as exc:
            raise BridgeError(f"bridge handshake failed: {ready[:200]!r}") from exc
        if not info.get("ok"):
            raise BridgeError(f"bridge refused: {info.get('error')}")

    def rpc(self, op: Dict[str, Any]) -> Dict[str, Any]:
        with self._rpc_lock:
            for attempt in (0, 1):
                if self._proc is None:
                    self._spawn()
                try:
                    self._proc.stdin.write(json.dumps(op) + "\n")
                    self._proc.stdin.flush()
                    line = self._proc.stdout.readline()
                    if not line:
                        raise BridgeError("bridge closed")
                    resp = json.loads(line)
                    if not resp.get("ok"):
                        raise BridgeError(str(resp.get("error")))
                    return resp
                except (BrokenPipeError, OSError, ValueError, BridgeError) as exc:
                    if attempt == 1:
                        raise BridgeError(f"bridge rpc failed ({op.get('op')}): {exc}") from exc
                    self._proc = None
        raise BridgeError("unreachable")


class EngineFacade:
    """Python-shaped facade over the Node bridge; mirrors the ODE surface the
    plugin hooks, dashboard API and /mind command rely on."""

    class _IrShim:
        def __init__(self, traits: Dict[str, Any]):
            self.traits = traits

    def __init__(self, bridge: Optional[NodeBridge]):
        self.bridge = bridge
        self.degraded = bridge is None
        self.state: Dict[str, float] = {}
        self.traits: Dict[str, float] = {}
        self.params: Dict[str, float] = {}
        self.sim_time: float = 0.0
        self.dt: float = 1.0
        self.solver: str = "euler"
        if self.degraded:
            self.ir = self._IrShim({})
            return
        meta = bridge.rpc({"op": "meta"})
        self.dt = float(meta.get("dt", 1.0))
        self.solver = str(meta.get("solver", "euler"))
        trait_meta = {}
        for name, t in (meta.get("traits") or {}).items():
            trait_meta[name] = type("T", (), {"min_val": float(t["min"]), "max_val": float(t["max"])})()
        self.ir = self._IrShim(trait_meta)
        self.traits = {k: float(t["default"]) for k, t in (meta.get("traits") or {}).items()}
        blob = bridge.rpc({"op": "export"})["blob"]
        self.state = dict(blob.get("state", {}))
        self.traits.update(blob.get("traits", {}))
        self.params = dict(blob.get("params", {}))

    def step(self, n: int = 1) -> Dict[str, float]:
        if self.degraded:
            return {}
        resp = self.bridge.rpc({"op": "step", "n": int(n)})
        self.state = resp.get("state", self.state)
        self.sim_time = float(resp.get("sim_time", self.sim_time))
        return resp.get("frame", {})

    def snapshot(self) -> Dict[str, float]:
        if self.degraded:
            return {}
        return self.bridge.rpc({"op": "snapshot"}).get("frame", {})

    def render(self, name: str = "ambient_mind", snap_override: Optional[Dict[str, float]] = None) -> str:
        if self.degraded:
            return ""
        op: Dict[str, Any] = {"op": "render", "name": name}
        if snap_override:
            op["frame"] = snap_override
        return str(self.bridge.rpc(op).get("text", ""))

    def set_leaf(self, path: str, value: float) -> float:
        if self.degraded:
            return 0.0
        v = float(self.bridge.rpc({"op": "set", "path": path, "value": float(value)}).get("value", value))
        self.state[path] = v
        return v

    def nudge(self, path: str, delta: float) -> float:
        if self.degraded:
            return 0.0
        v = float(self.bridge.rpc({"op": "nudge", "path": path, "delta": float(delta)}).get("value", 0.0))
        self.state[path] = v
        return v

    def apply_preset(self, name: str, scale: float = 1.0) -> None:
        if self.degraded:
            return
        resp = self.bridge.rpc({"op": "preset", "name": name, "scale": float(scale)})
        frame = resp.get("frame") or {}
        self.state.update(frame)

    def set_param(self, name: str, value: float) -> None:
        if self.degraded:
            return
        try:
            self.bridge.rpc({"op": "setParam", "name": name, "value": float(value)})
            self.params[name] = float(value)
        except BridgeError:
            pass  # unknown param name is fine; model may not define it

    def set_trait(self, name: str, value: float) -> None:
        if self.degraded:
            return
        self.bridge.rpc({"op": "setTrait", "name": name, "value": float(value)})
        self.traits[name] = float(value)

    def export_blob(self) -> Dict[str, Any]:
        if self.degraded:
            return {"schema": "mind-hermes-state-v4", "sim_time": 0.0, "state": {}, "traits": {}, "params": {}}
        return self.bridge.rpc({"op": "export"})["blob"]

    def import_blob(self, blob: Dict[str, Any]) -> None:
        if self.degraded:
            return
        # never let a stale saved blob clobber freshly synced wall-clock params
        params = {k: v for k, v in (blob.get("params") or {}).items()
                  if k not in ("env.start_doy", "clock.start_hour")}
        blob = {**blob, "params": params}
        self.bridge.rpc({"op": "import", "blob": blob})
        self.state = dict(blob.get("state", self.state))
        self.traits.update(blob.get("traits", {}))
        self.params.update(params)
        self.sim_time = float(blob.get("sim_time", self.sim_time))


class MindRuntimeSingleton:
    """Thread-safe & multi-process-synced ODE runtime shared by Hermes hooks & Web Dashboard."""

    def __init__(self, model_path: Optional[Path] = None):
        self.lock = threading.RLock()
        self.model_path = model_path or DEFAULT_MODEL_PATH
        self.state_path = get_data_dir() / "runtime_state.json"

        try:
            bridge: Optional[NodeBridge] = NodeBridge(self.model_path)
        except BridgeError as exc:
            print(f"[mind-hermes] engine degraded mode: {exc}")
            bridge = None
        self.engine = EngineFacade(bridge)
        self.sensors = TelemetryCollector(
            SensorConfig(latitude=self.engine.params.get("env.lat_deg", 48.2))
        )

        self.gateway_url = os.environ.get("HERMES_GATEWAY_URL", "http://127.0.0.1:8642")
        self.outreach_webhook = os.environ.get("HERMES_OUTREACH_WEBHOOK", "")

        self._last_disk_mtime: float = 0.0
        self._prev_outreach_fire = 0.0
        self._prev_bg_fire = 0.0
        self._pending_wake_queue: List[Dict[str, Any]] = []
        self._wake_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._ticker_started = False

        self._sync_wall_clock_and_restore()

    def apply_plugin_config(self, cfg: Dict[str, Any]) -> None:
        """Applies user settings from Hermes `plugin.yaml` config if provided."""
        if not cfg:
            return
        with self.lock:
            sc = self.sensors.cfg
            if cfg.get("obsidian_vault_path"):
                sc.obsidian_vault_path = str(cfg["obsidian_vault_path"])
            if cfg.get("honcho_inbox_subdir"):
                sc.honcho_inbox_subdir = str(cfg["honcho_inbox_subdir"])
            if cfg.get("honcho_url"):
                sc.honcho_url = str(cfg["honcho_url"])
            if cfg.get("ha_base_url"):
                sc.ha_base_url = str(cfg["ha_base_url"])
            if cfg.get("ha_token"):
                sc.ha_token = str(cfg["ha_token"])
            if cfg.get("ha_solar_entity"):
                sc.ha_solar_entity = str(cfg["ha_solar_entity"])
            if cfg.get("ha_battery_entity"):
                sc.ha_battery_entity = str(cfg["ha_battery_entity"])
            if cfg.get("solar_max_kw") is not None:
                sc.solar_max_kw = float(cfg["solar_max_kw"])
            if cfg.get("hwmon_coolant_path"):
                sc.hwmon_coolant_path = str(cfg["hwmon_coolant_path"])
            if cfg.get("use_liquidctl") is not None:
                sc.use_liquidctl = bool(cfg["use_liquidctl"])
            if cfg.get("latitude") is not None:
                sc.latitude = float(cfg["latitude"])
                self.engine.set_param("env.lat_deg", sc.latitude)
            if cfg.get("longitude") is not None:
                sc.longitude = float(cfg["longitude"])
                self.engine.set_param("env.lon_deg", sc.longitude)
            if cfg.get("gateway_url"):
                self.gateway_url = str(cfg["gateway_url"])
            if cfg.get("outreach_webhook"):
                self.outreach_webhook = str(cfg["outreach_webhook"])

    def start_background_ticker(self, interval_sec: float = 60.0) -> None:
        with self.lock:
            if self._ticker_started:
                return
            self._ticker_started = True

        def _loop() -> None:
            while True:
                time.sleep(interval_sec)
                try:
                    self.tick_once()
                except Exception as exc:
                    print(f"[mind-hermes] Tick error: {exc}")

        t = threading.Thread(target=_loop, name="mind-hermes-ode-ticker", daemon=True)
        t.start()

    def register_wake_callback(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        with self.lock:
            self._wake_callbacks.append(fn)

    def drain_pending_wakes(self) -> List[Dict[str, Any]]:
        """Pop the wake backlog accumulated between turns (consumed by pre_llm_call context)."""
        with self.lock:
            drained = self._pending_wake_queue
            self._pending_wake_queue = []
            return drained

    def _sync_wall_clock_and_restore(self) -> None:
        now_dt = datetime.datetime.now()
        self.engine.set_param("env.start_doy", float(now_dt.timetuple().tm_yday))
        self.engine.set_param("clock.start_hour", float(now_dt.hour + now_dt.minute / 60.0))

        if not self.state_path.exists():
            return

        try:
            self._last_disk_mtime = self.state_path.stat().st_mtime
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            blob = saved.get("mind_state")
            if blob:
                self.engine.import_blob(blob)

            last_ts = float(saved.get("wall_timestamp", time.time()))
            downtime_min = max(0.0, (time.time() - last_ts) / 60.0)

            if downtime_min > 20.0:
                shock = min(0.95, 0.25 + math.log1p(downtime_min / 30.0) * 0.28)
                self.engine.set_leaf("sensors.stasis_shock", shock)
                self.engine.set_leaf("social.user_present", 0.0)
                self.engine.nudge("social.away_days", downtime_min / 1440.0)
                self.engine.step(min(60, int(downtime_min)))
        except Exception as exc:
            print(f"[mind-hermes] Restore warning: {exc}")

    def reload_from_disk_if_newer(self) -> None:
        if not self.state_path.exists():
            return
        try:
            mtime = self.state_path.stat().st_mtime
            if mtime <= self._last_disk_mtime:
                return
            with self.lock:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                blob = saved.get("mind_state")
                if blob:
                    self.engine.import_blob(blob)
                self._last_disk_mtime = mtime
        except Exception:
            pass

    def save_state(self) -> None:
        with self.lock:
            payload = {
                "wall_timestamp": time.time(),
                "mind_state": self.engine.export_blob(),
            }
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.state_path)
            try:
                self._last_disk_mtime = self.state_path.stat().st_mtime
            except OSError:
                pass

    def tick_once(self) -> Dict[str, float]:
        self.reload_from_disk_if_newer()
        live_readings = self.sensors.collect_all()
        with self.lock:
            for path, val in live_readings.items():
                if path in self.engine.state:
                    cur = self.engine.state[path]
                    self.engine.set_leaf(path, cur + 0.35 * (val - cur))

            self.engine.step(1)
            snap = self.engine.snapshot()
            self.save_state()

            outreach_now = snap.get("initiative.fire_outreach", 0.0)
            bg_now = snap.get("initiative.fire_background", 0.0)

            if outreach_now > 0.5 and self._prev_outreach_fire <= 0.5:
                self._emit_wake("user_outreach", snap)
            if bg_now > 0.5 and self._prev_bg_fire <= 0.5:
                self._emit_wake("silent_worker", snap)

            self._prev_outreach_fire = outreach_now
            self._prev_bg_fire = bg_now
            return snap

    def _emit_wake(self, mode: str, snap: Dict[str, float]) -> None:
        event = {
            "wake_mode": mode,
            "timestamp": time.time(),
            "ambient_mind": self.engine.render("ambient_mind", snap),
            "state_telemetry": self.engine.render("state_telemetry", snap),
        }
        self._pending_wake_queue.append(event)
        if len(self._pending_wake_queue) > 20:
            del self._pending_wake_queue[:-20]
        for cb in list(self._wake_callbacks):
            try:
                cb(event)
            except Exception as exc:
                print(f"[mind-hermes] Wake callback error: {exc}")

        threading.Thread(
            target=self._call_hermes_gateway_on_wake,
            args=(event,),
            daemon=True,
        ).start()

    def _call_hermes_gateway_on_wake(self, event: Dict[str, Any]) -> None:
        gateway_url = self.gateway_url.rstrip("/")
        api_key = os.environ.get("API_SERVER_KEY", "")
        if not api_key:
            env_file = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / ".env"
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.strip().startswith("API_SERVER_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break

        mode = event["wake_mode"]
        if mode == "silent_worker":
            prompt = (
                f"{event['ambient_mind']}\n\n"
                "[SILENT WORKER WAKE: The user is AFK. Do NOT send a chat notification. "
                "Use your Honcho and Obsidian tools to consolidate unindexed notes, merge session "
                "LCM pointers into permanent concept notes, and organize open project tasks.]"
            )
        else:
            prompt = (
                f"{event['ambient_mind']}\n\n"
                "[PROACTIVE OUTREACH WAKE: You woke up with a strong internal urge to reach out "
                "to the user. Write a natural, unprompted message reflecting what is on your mind "
                "(an Obsidian eureka insight, an open project loop, or missing the user).]"
            )

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body = json.dumps({
            "model": os.environ.get("HERMES_WAKE_MODEL", "auto"),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                f"{gateway_url}/v1/chat/completions",
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                reply_text = (
                    result.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                notify_url = self.outreach_webhook
                if mode == "user_outreach" and reply_text and notify_url:
                    n_req = urllib.request.Request(
                        notify_url,
                        data=json.dumps({"text": reply_text, "content": reply_text}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(n_req, timeout=10.0)
        except Exception as exc:
            print(f"[mind-hermes] Gateway wake request skipped/failed ({mode}): {exc}")

    def status_dict(self) -> Dict[str, Any]:
        with self.lock:
            snap = self.engine.snapshot()
            spont = snap.get("entropy.spontaneity", 0.3)
            return {
                "sim_time": self.engine.sim_time,
                "degraded": self.engine.degraded,
                "ambient_mind": self.engine.render("ambient_mind", snap),
                "state_telemetry": self.engine.render("state_telemetry", snap),
                "sampling_temperature_hint": round(0.55 + 0.50 * spont, 2),
                "wake": {
                    "fire_outreach": snap.get("initiative.fire_outreach", 0.0),
                    "fire_background": snap.get("initiative.fire_background", 0.0),
                    "pending_wake": self._pending_wake_queue[-1] if self._pending_wake_queue else None,
                },
                "frame": snap,
                "state": dict(self.engine.state),
                "traits": dict(self.engine.traits),
                "params": dict(self.engine.params),
            }


_SINGLETON: Optional[MindRuntimeSingleton] = None
_SINGLETON_LOCK = threading.Lock()


def get_mind_runtime() -> MindRuntimeSingleton:
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = MindRuntimeSingleton()
        return _SINGLETON
