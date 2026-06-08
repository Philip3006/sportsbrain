import functools
import pickle
import time
from pathlib import Path

from src.config import DATA_CACHE


def disk_cache(name: str, max_age_hours: float = 24.0):
    """
    Decorator for zero-argument callables. Persists result at DATA_CACHE/{name}.pkl.
    Wrapped function receives an optional `force: bool = False` kwarg to bypass cache.
    DATA_CACHE is read at call time so tests can monkeypatch it to a temp directory.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, force: bool = False, **kwargs):
            cache_path = DATA_CACHE / f"{name}.pkl"
            if not force and cache_path.exists():
                age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
                if age_hours < max_age_hours:
                    with open(cache_path, "rb") as f:
                        return pickle.load(f)

            result = fn(*args, **kwargs)
            DATA_CACHE.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump(result, f)
            return result

        return wrapper

    return decorator


def clear_cache(name: str) -> bool:
    path = DATA_CACHE / f"{name}.pkl"
    if path.exists():
        path.unlink()
        return True
    return False
