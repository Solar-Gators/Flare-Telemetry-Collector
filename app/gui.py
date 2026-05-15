import sys
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Telemetry Collector")

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Telemetry Collector"))
        self.setLayout(layout)


def run_app():
    app = QApplication(sys.argv)

    window = MainWindow()
    window.resize(500, 300)
    window.show()

    sys.exit(app.exec())