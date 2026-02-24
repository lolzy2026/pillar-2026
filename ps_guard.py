import asyncio
import psutil
import os
import logging
from functools import wraps

process = psutil.Process(os.getpid())

def cpu_spike_guard(threshold=80, interval=0.05):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):

            spike_detected = False
            running = True

            async def sampler():
                nonlocal spike_detected
                while running:
                    cpu = process.cpu_percent(None)
                    if cpu > threshold:
                        spike_detected = True
                        logging.warning(
                            f"🚨 CPU spike {cpu:.1f}% inside {func.__name__}"
                        )
                    await asyncio.sleep(interval)

            sampler_task = asyncio.create_task(sampler())

            try:
                return await func(*args, **kwargs)
            finally:
                running = False
                await sampler_task

                if spike_detected:
                    logging.warning(f"⚠️ Spike occurred during {func.__name__}")

        return wrapper
    return decorator
