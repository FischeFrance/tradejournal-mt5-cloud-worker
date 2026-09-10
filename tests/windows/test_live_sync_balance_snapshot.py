from types import SimpleNamespace

from windows_agent.worker.live_sync import LiveSync


class SnapshotState:
    def __init__(self):
        self.value = {"positions": {}, "orders": {}, "deals": {}}

    def get(self):
        return self.value

    def save(self, value):
        self.value = value


class Adapter:
    def verify_identity(self):
        return {"login": "12345", "server": "Demo"}

    def snapshot(self):
        return {
            "positions": {
                "1": {
                    "ticket": "1",
                    "symbol": "EURUSD",
                    "direction": "buy",
                    "volume": 0.1,
                    "open_price": 1.1,
                }
            },
            "orders": {},
            "deals": {},
        }

    def account_snapshot(self):
        return {
            "balance": 9_768.56,
            "equity": 9_770.12,
            "currency": "USD",
            "leverage": 100,
        }


class Dedup:
    @staticmethod
    def contains(_event_id):
        return False

    @staticmethod
    def add(_event_id):
        return True


class Outbox:
    def __init__(self):
        self.pending = []

    def enqueue_many(self, payloads):
        self.pending.extend(payloads)

    def drain(self, sender):
        sent = len(self.pending)
        for payload in self.pending:
            sender.send(payload)
        self.pending.clear()
        return SimpleNamespace(
            sent=sent,
            pending=0,
            dead_lettered=0,
            permanent_failures=0,
            dry_run=0,
        )

    @staticmethod
    def dead_letter_count():
        return 0


def test_live_event_carries_the_latest_account_balance():
    delivered = []
    sync = LiveSync(
        Adapter(),
        SnapshotState(),
        Dedup(),
        delivered.append,
        outbox=Outbox(),
    )

    assert sync.poll_once() == 1
    assert len(delivered) == 1
    assert delivered[0]["event_type"] == "trade_opened"
    assert delivered[0]["balance"] == 9_768.56
    assert delivered[0]["equity"] == 9_770.12
    assert delivered[0]["currency"] == "USD"
    assert delivered[0]["leverage"] == 100
