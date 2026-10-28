import threading
from contextlib import contextmanager


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    """Reserve before work. Credits use conservative local token-equivalent units."""

    def __init__(self, max_agents: int, credits: int, on_reserve=None):
        self.max_agents, self.remaining = max_agents, credits
        self.active = self.peak = self.spent = 0
        self.lock = threading.Lock()
        self.on_reserve = on_reserve

    def reserve(self, units: int):
        with self.lock:
            if units < 0 or units > self.remaining:
                raise BudgetExceeded("API credit reservation exceeds remaining tick budget")
            if self.on_reserve:
                self.on_reserve(units)  # Commit durable spend before the external call.
            self.remaining -= units
            self.spent += units

    @contextmanager
    def agents(self, count: int):
        with self.lock:
            if count < 1 or self.active + count > self.max_agents:
                raise BudgetExceeded("Active agent limit would be exceeded")
            self.active += count
            self.peak = max(self.peak, self.active)
        try:
            yield
        finally:
            with self.lock:
                self.active -= count
