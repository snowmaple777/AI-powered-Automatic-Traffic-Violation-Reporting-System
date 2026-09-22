"""Stable data contract between perception models and violation rules."""

SCHEMA_VERSION = "1.2"


def build_frame_observation(frame_index, timestamp_sec, width, height, fps,
                            vehicles, traffic_lights, road_markings,
                            depth=None, plates=None, enabled_models=None):
    """Return the only input shape consumed by violation rules."""
    return {
        "schema_version": SCHEMA_VERSION,
        "frame": {
            "index": frame_index,
            "timestamp_sec": round(timestamp_sec, 3),
            "width": width,
            "height": height,
            "fps": round(fps, 5),
        },
        "observations": {
            "vehicles": vehicles,
            "traffic_lights": traffic_lights,
            "road_markings": road_markings,
            "depth": depth or [],
            "plates": plates or [],
        },
        "enabled_models": enabled_models or [],
        "postprocessing": {"road_marking_compensation": {"enabled": False}},
    }
