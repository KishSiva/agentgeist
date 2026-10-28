import json


class RunFailed(RuntimeError):
    """Preserve a run receipt without exposing the underlying provider exception."""

    def __init__(self, error_type, run_id, notification):
        self.run_id = run_id
        self.run_notification = {key: value for key, value in notification.items()
                                 if key in {"status", "run_id", "channel_id", "message_id", "message_url", "delivery"}}
        super().__init__("Execution failed: " + json.dumps({
            "error_type": error_type, "run_id": run_id, "run_notification": self.run_notification,
        }))
