import asyncio
import functools
import secrets


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[BaseException], ...] = (OSError,),
):
    """Retry an async function with exponential backoff and ±25% jitter.

    max_attempts=0 retries indefinitely.
    Exceptions not listed in `exceptions` propagate immediately without retry.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except exceptions:
                    attempt += 1
                    if max_attempts and attempt >= max_attempts:
                        raise
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    jitter = delay * (secrets.randbelow(51) / 100 - 0.25)
                    await asyncio.sleep(max(0.0, delay + jitter))

        return wrapper

    return decorator
