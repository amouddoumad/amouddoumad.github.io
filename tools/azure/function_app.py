"""Azure Functions v4 (Python) entry point for the data.json backup runner.
All logic lives in backup.py so it stays testable without the Azure SDK."""

import logging

import azure.functions as func

import backup

app = func.FunctionApp()


@app.timer_trigger(name="tick", schedule="0 */10 * * * *", run=True)
def backup_timer(tick: func.TimerRequest) -> None:
    def log(msg):
        logging.info(msg)

    try:
        backup.run_once(log=log)
    except Exception:  # noqa: BLE001
        logging.exception("backup tick failed")
