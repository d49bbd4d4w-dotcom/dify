"""Free, reliable weather data: National Weather Service and Open-Meteo.

- NWS forecast (api.weather.gov): the official high-temperature forecast for the
  spot Kalshi settles on.
- NWS observations: what the settlement station has actually recorded today.
- Open-Meteo ensembles: ~80 runs of the US (GFS) and European (ECMWF) weather
  models, which show how uncertain the forecast is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from . import Http

NWS = "https://api.weather.gov"
ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODELS = ("gfs_seamless", "ecmwf_ifs025")


@dataclass(frozen=True)
class Station:
    name: str
    station_id: str  # NWS station whose daily climate report settles the market
    lat: float
    lon: float
    tz: str


# Kalshi daily-high series -> settlement station. Check a market's rules in the
# Kalshi app if in doubt; the alert always names the station used.
STATIONS: dict[str, Station] = {
    "KXHIGHNY": Station("New York (Central Park)", "KNYC", 40.7790, -73.9693, "America/New_York"),
    "KXHIGHCHI": Station("Chicago (Midway)", "KMDW", 41.7860, -87.7524, "America/Chicago"),
    "KXHIGHMIA": Station("Miami (Miami Intl)", "KMIA", 25.7881, -80.3169, "America/New_York"),
    "KXHIGHAUS": Station("Austin (Bergstrom)", "KAUS", 30.1831, -97.6799, "America/Chicago"),
    "KXHIGHLAX": Station("Los Angeles (LAX)", "KLAX", 33.9382, -118.3866, "America/Los_Angeles"),
    "KXHIGHDEN": Station("Denver (Denver Intl)", "KDEN", 39.8466, -104.6562, "America/Denver"),
    "KXHIGHPHIL": Station("Philadelphia (PHL)", "KPHL", 39.8733, -75.2268, "America/New_York"),
}


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def lowest_f(c: float) -> int:
    """Lowest whole-°F temperature consistent with an observation in °C.

    Stations measure whole °F and report °C to a tenth, so a reading like 18.3
    maps back exactly (18.3°C -> 65°F). A whole-°C reading may have been rounded
    to the nearest degree C, so take the lowest °F it could have been.
    """
    if abs(c - round(c)) > 1e-6:
        return round(c_to_f(c))
    return math.ceil(c_to_f(c - 0.5) - 1e-9)


class Weather:
    def __init__(self, http: Http):
        self.http = http
        self._forecast_urls: dict[tuple[float, float], str] = {}

    def nws_high(self, st: Station, day: date) -> int | None:
        """NWS forecast high (°F) for the daytime period on `day`."""
        key = (st.lat, st.lon)
        if key not in self._forecast_urls:
            pts = self.http.get_json(f"{NWS}/points/{st.lat:.4f},{st.lon:.4f}")
            self._forecast_urls[key] = pts["properties"]["forecast"]
        data = self.http.get_json(self._forecast_urls[key])
        for p in data["properties"]["periods"]:
            if p.get("isDaytime") and p["startTime"][:10] == day.isoformat():
                t = float(p["temperature"])
                return round(c_to_f(t)) if p.get("temperatureUnit") == "C" else round(t)
        return None

    def observed_high(self, st: Station, start: datetime, end: datetime) -> int | None:
        """Highest temperature (whole °F, never overstated) the station has
        recorded between start and end."""
        data = self.http.get_json(f"{NWS}/stations/{st.station_id}/observations",
                                  {"start": start.isoformat(), "end": end.isoformat()})
        temps = [f["properties"]["temperature"]["value"] for f in data.get("features", [])]
        temps = [lowest_f(float(t)) for t in temps if t is not None]
        return max(temps) if temps else None

    def ensemble_highs(self, st: Station, day: date) -> list[float]:
        """Daily high (°F) from every member of the GFS and ECMWF ensembles."""
        out: list[float] = []
        for model in ENSEMBLE_MODELS:
            data = self.http.get_json(ENSEMBLE, {
                "latitude": st.lat, "longitude": st.lon, "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit", "timezone": st.tz, "models": model, "forecast_days": 4})
            daily = data.get("daily", {})
            days = daily.get("time", [])
            if day.isoformat() not in days:
                continue
            i = days.index(day.isoformat())
            for key, values in daily.items():
                if key.startswith("temperature_2m_max") and i < len(values) and values[i] is not None:
                    out.append(float(values[i]))
        return out
