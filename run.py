# -*- coding: utf-8 -*-
from app import create_app

app = create_app()

if __name__ == '__main__':
    # V1 is intentionally single-process: authentication throttling and active
    # AI-run cancellation are bounded in-process registries.
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)
