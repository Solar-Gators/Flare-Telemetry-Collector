# main.py

import sys
from PySide6.QtWidgets import QApplication

from app.gui import TelemetryWindow
from app.telemetry_state import TelemetryState
from app.telemetry_receiver import TelemetryReceiver


PORT = "COM4"
BAUD = 57600


def main():
    state = TelemetryState()

    receiver = TelemetryReceiver(
        port=PORT,
        baud=BAUD,
        state=state,
    )
    receiver.start()

    app = QApplication(sys.argv)

    win = TelemetryWindow(state)
    win.showFullScreen()

    exit_code = app.exec()

    receiver.stop()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()