from flask import Flask

app = Flask(__name__)


@app.route('/')
def webhook():
    return 'ok', 200


def some_function():
    pass
