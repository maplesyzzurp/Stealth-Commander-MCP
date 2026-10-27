import sys
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

app = QApplication(sys.argv)
window = QWidget()
window.setWindowTitle('Harness example')
layout = QVBoxLayout(window)
name = QLineEdit()
name.setAccessibleName('Value')
result = QLabel('Enter a value and press Apply.')
greet = QPushButton('Apply')
greet.clicked.connect(lambda: result.setText('Result: ' + name.text() + '!'))
for control in (name, greet, result):
    layout.addWidget(control)
window.resize(420, 180)
window.show()
sys.exit(app.exec())
