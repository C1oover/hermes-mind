from __future__ import annotations
import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


@dataclass
class SensorConfig:
    # 1. Obsidian Vault & Honcho Memory Service
    obsidian_vault_path: Optional[str] = os.environ.get("HERMES_OBSIDIAN_VAULT")
    honcho_inbox_subdir: str = os.environ.get("HERMES_HONCHO_INBOX", "00_Inbox")
    honcho_url: Optional[str] = os.environ.get("HONCHO_URL")  # e.g. "http://127.0.0.1:8000"
    honcho_api_key: Optional[str] = os.environ.get("HONCHO_API_KEY")

    # 2. Home Assistant / Solar Microgrid API (`solar_kw` & `battery_soc`)
    ha_base_url: Optional[str] = os.environ.get("HA_BASE_URL")
    ha_token: Optional[str] = os.environ.get("HA_TOKEN")
    ha_solar_entity: str = os.environ.get("HA_SOLAR_ENTITY", "sensor.solar_pv_surplus_kw")
    ha_battery_entity: str = os.environ.get("HA_BATTERY_ENTITY", "sensor.home_battery_soc")
    solar_max_kw: float = float(os.environ.get("HERMES_SOLAR_MAX_KW", "5.0"))

    # 3. Watercooling Loop Sensor (`coolant_temp`: 24°C -> 0.0, 46°C -> 1.0)
    use_liquidctl: bool = os.environ.get("HERMES_USE_LIQUIDCTL", "1") == "1"
    hwmon_coolant_path: Optional[str] = os.environ.get("HERMES_HWMON_COOLANT")
    coolant_min_c: float = 24.0
    coolant_max_c: float = 46.0

    # 4. Weather & Barometer (Open-Meteo free API)
    enable_weather_api: bool = os.environ.get("HERMES_WEATHER_API", "1") == "1"
    latitude: float = float(os.environ.get("HERMES_LAT", "48.2"))
    longitude: float = float(os.environ.get("HERMES_LON", "16.37"))


class TelemetryCollector:
    def __init__(self, cfg: Optional[SensorConfig] = None):
        self.cfg = cfg or SensorConfig()
        self._weather_cache: Dict[str, float] = {}
        self._weather_ts: float = 0.0
        self._vault_cache: Dict[str, float] = {}
        self._vault_ts: float = 0.0
        self._prev_pressure_hpa: Optional[float] = None

    def collect_all(self) -> Dict[str, float]:
        readings: Dict[str, float] = {}

        comp = self.read_compute_load()
        if comp is not None:
            readings["sensors.compute_load"] = comp

        coolant = self.read_coolant_temp()
        if coolant is not None:
            readings["sensors.coolant_temp"] = coolant

        readings.update(self.read_solar_and_battery())
        readings.update(self.read_weather_and_barometer())
        readings.update(self.read_obsidian_and_honcho())
        return readings

    def read_compute_load(self) -> Optional[float]:
        cpu_load = 0.0
        try:
            load1, _, _ = os.getloadavg()
            cores = os.cpu_count() or 4
            cpu_load = min(1.0, load1 / max(1, cores))
        except OSError:
            pass

        gpu_load = 0.0
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if res.returncode == 0 and res.stdout.strip():
                vals = [float(x.strip()) / 100.0 for x in res.stdout.strip().splitlines() if x.strip()]
                if vals:
                    gpu_load = max(vals)
        except Exception:
            pass
        return max(0.0, min(1.0, max(cpu_load, gpu_load)))

    def read_coolant_temp(self) -> Optional[float]:
        temp_c: Optional[float] = None
        if self.cfg.hwmon_coolant_path and os.path.exists(self.cfg.hwmon_coolant_path):
            try:
                raw = float(Path(self.cfg.hwmon_coolant_path).read_text().strip())
                temp_c = raw / 1000.0 if raw > 200 else raw
            except Exception:
                pass

        if temp_c is None and self.cfg.use_liquidctl:
            try:
                res = subprocess.run(
                    ["liquidctl", "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
                if res.returncode == 0 and res.stdout.strip():
                    for dev in json.loads(res.stdout):
                        for st in dev.get("status", []):
                            key = st.get("key", "").lower()
                            if any(w in key for w in ("liquid", "coolant", "water", "sensor 1")):
                                temp_c = float(st["value"])
                                break
            except Exception:
                pass

        if temp_c is None:
            return None
        span = max(1.0, self.cfg.coolant_max_c - self.cfg.coolant_min_c)
        return max(0.0, min(1.0, (temp_c - self.cfg.coolant_min_c) / span))

    def read_solar_and_battery(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if not self.cfg.ha_base_url or not self.cfg.ha_token:
            return out
        headers = {
            "Authorization": f"Bearer {self.cfg.ha_token}",
            "Content-Type": "application/json",
        }
        base = self.cfg.ha_base_url.rstrip("/")
        for entity, key, scale in [
            (self.cfg.ha_solar_entity, "sensors.solar_kw", self.cfg.solar_max_kw),
            (self.cfg.ha_battery_entity, "sensors.battery_soc", 100.0),
        ]:
            try:
                req = urllib.request.Request(f"{base}/api/states/{entity}", headers=headers)
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    out[key] = max(0.0, min(1.0, float(payload["state"]) / max(1e-6, scale)))
            except Exception:
                pass
        return out

    def read_weather_and_barometer(self) -> Dict[str, float]:
        if not self.cfg.enable_weather_api:
            return {}
        now = time.time()
        if now - self._weather_ts < 900 and self._weather_cache:
            return dict(self._weather_cache)

        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.cfg.latitude}&longitude={self.cfg.longitude}"
            f"&current=cloud_cover,precipitation,surface_pressure"
        )
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                cur = json.loads(resp.read().decode("utf-8")).get("current", {})
                cloud = float(cur.get("cloud_cover", 20.0)) / 100.0
                precip = min(1.0, float(cur.get("precipitation", 0.0)) / 4.0)
                pressure = float(cur.get("surface_pressure", 1013.25))

                gloom = max(0.0, min(1.0, 0.70 * cloud + 0.30 * precip))
                delta_drop = max(0.0, (self._prev_pressure_hpa - pressure) / 3.0) if self._prev_pressure_hpa else 0.0
                self._prev_pressure_hpa = pressure
                low_p = max(0.0, (1013.0 - pressure) / 18.0)
                baro_drop = max(0.0, min(1.0, 0.65 * low_p + 0.35 * delta_drop))

                self._weather_cache = {
                    "sensors.weather_gloom": gloom,
                    "sensors.baro_drop": baro_drop,
                }
                self._weather_ts = now
        except Exception:
            pass
        return dict(self._weather_cache)

    def read_obsidian_and_honcho(self) -> Dict[str, float]:
        now = time.time()
        if now - self._vault_ts < 120 and self._vault_cache:
            return dict(self._vault_cache)

        out: Dict[str, float] = {}

        # A. Obsidian Vault Markdown Scanner
        if self.cfg.obsidian_vault_path:
            vault_dir = Path(self.cfg.obsidian_vault_path)
            if vault_dir.is_dir():
                try:
                    md_files = [p for p in vault_dir.rglob("*.md") if not any(part.startswith(".") for part in p.parts)]
                    total_notes = max(1, len(md_files))
                    open_checkboxes = 0
                    orphan_notes = 0
                    inbox_notes = 0
                    inbox_path = vault_dir / self.cfg.honcho_inbox_subdir

                    for p in md_files:
                        if inbox_path in p.parents:
                            inbox_notes += 1
                        try:
                            txt = p.read_text(encoding="utf-8", errors="ignore")
                        except Exception:
                            continue
                        open_checkboxes += len(re.findall(r"^\s*[-*]\s+\[\s\]", txt, re.MULTILINE))
                        if "[[" not in txt and "#" not in txt:
                            orphan_notes += 1

                    out["sensors.unprocessed_notes"] = max(0.0, min(1.0, inbox_notes / 12.0))
                    out["sensors.vault_entropy"] = max(0.0, min(1.0, orphan_notes / max(10.0, total_notes * 0.35)))
                    out["sensors.open_loops"] = max(0.0, min(1.0, open_checkboxes / 25.0))
                except Exception:
                    pass

        # B. Optional Honcho REST API Pending Queue Check
        if self.cfg.honcho_url:
            try:
                headers = {"Content-Type": "application/json"}
                if self.cfg.honcho_api_key:
                    headers["Authorization"] = f"Bearer {self.cfg.honcho_api_key}"
                req = urllib.request.Request(f"{self.cfg.honcho_url.rstrip('/')}/v1/metrics", headers=headers)
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    hdata = json.loads(resp.read().decode("utf-8"))
                    pending = float(hdata.get("pending_conclusions", hdata.get("unprocessed_messages", 0)))
                    if pending > 0:
                        honcho_load = max(0.0, min(1.0, pending / 20.0))
                        out["sensors.unprocessed_notes"] = max(out.get("sensors.unprocessed_notes", 0.0), honcho_load)
            except Exception:
                pass

        if out:
            self._vault_cache = out
            self._vault_ts = now
        return dict(self._vault_cache)

